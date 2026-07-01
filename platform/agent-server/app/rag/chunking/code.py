"""tree-sitter based code chunker — the foundation of our RAG pipeline.

HOW IT FITS IN THE RAG PIPELINE:
  Source files → [chunker.py] → Chunks → [embedder.py] → vectors → [retriever.py] → search results

WHY tree-sitter OVER SIMPLER APPROACHES?
- Regex-based chunking (split on "def ", "class ") is fragile — misses edge cases
  like decorators, nested classes, multi-line signatures, and language differences.
- Fixed-size chunking (every 50 lines) splits functions in half, losing semantic meaning.
- tree-sitter parses the ACTUAL AST (Abstract Syntax Tree), giving us complete
  functions, classes, and methods as single chunks — the natural unit of code meaning.

HOW tree-sitter WORKS (for newbies):
1. tree-sitter is a parser generator tool (written in C, with Python bindings)
2. Each language has a grammar file that describes its syntax rules
3. tree-sitter builds a concrete syntax tree from source code — every token
   is represented as a node in the tree
4. We walk the tree looking for specific node types (function_definition, class_definition)
5. For each matching node, we extract the FULL text from start_byte to end_byte
6. This gives us syntactically complete code units — never a partial function

THREADING MODEL:
- tree-sitter parsing is CPU-bound (C code, no I/O)
- chunk_file() is synchronous — blocks the calling thread
- chunk_directory() is also synchronous — calls chunk_file() in a loop
- When called from the async indexer, it runs in the event loop thread
  (acceptable because parsing is fast: ~1ms per file)
- For very large codebases (10K+ files), consider running in a thread pool

MEMORY MODEL:
- tree-sitter creates an in-memory tree for each file, then discards it
- Peak memory = largest single file's AST (~10x file size)
- Chunks are stored as Python objects (Chunk dataclass)
- 674 chunks × ~500 bytes/chunk content = ~337KB
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Language-specific tree-sitter bindings
# Each import loads a compiled C parser for that language
# These are ~1MB each in memory and loaded once at module import time
import tree_sitter_python as tspython
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

# ── Language Registry ─────────────────────────────────────────
# Maps file extension → tree-sitter Language object
# Language() wraps the C parser — it's a thin Python binding
# Adding a new language: pip install tree-sitter-{lang}, add to this dict
LANGUAGES = {
    ".py": Language(tspython.language()),
    ".java": Language(tsjava.language()),
    ".js": Language(tsjs.language()),
    ".ts": Language(tsts.language_typescript()),
    ".tsx": Language(tsts.language_tsx()),
}

# ── AST Node Types to Extract ────────────────────────────────
# These are the tree-sitter node types that represent meaningful code units.
# Different languages use different node type names for the same concept:
# - Python: "function_definition" vs Java: "method_declaration"
# - Python: "class_definition" vs Java: "class_declaration"
# 
# To find node types for a new language: parse a sample file with tree-sitter,
# then print the AST tree — each node has a type string.
CHUNK_NODE_TYPES = {
    ".py": {"function_definition", "class_definition"},
    ".java": {"method_declaration", "class_declaration", "constructor_declaration", "interface_declaration"},
    ".js": {"function_declaration", "class_declaration", "method_definition", "arrow_function"},
    ".ts": {"function_declaration", "class_declaration", "method_definition", "arrow_function"},
    ".tsx": {"function_declaration", "class_declaration", "method_definition", "arrow_function"},
}

LANGUAGE_NAMES = {".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx"}


@dataclass
class Chunk:
    """A single semantic unit of code — one function, class, or method.
    
    This is the atomic unit that gets embedded and stored in the vector index.
    Each chunk should be self-contained: reading it alone should give you
    enough context to understand what this code does.
    
    DESIGN DECISION: We include the FULL text of each function/class, not just
    the signature. This is because the embedding model needs the body to understand
    what the function DOES, not just what it's called. "def process(data)" tells
    you nothing; the body tells you it's doing JSON parsing.
    """
    content: str             # Full source text of the chunk
    file_path: str           # Absolute path to the source file
    language: str            # "python", "java", "javascript", etc.
    chunk_type: str          # "function", "class", "method", "module"
    name: str                # Function/class name (e.g., "calculate_total")
    start_line: int          # 1-based line number where chunk starts
    end_line: int            # 1-based line number where chunk ends
    last_modified: datetime = field(default_factory=datetime.now)
    docstring: str | None = None    # Extracted docstring/javadoc (if any)
    metadata: dict = field(default_factory=dict)  # Extensible metadata


CodeChunk = Chunk  # Backward compatibility alias


def _get_node_name(node, source: bytes) -> str:
    """Extract the name identifier from a tree-sitter node."""
    for child in node.children:
        if child.type in ("identifier", "property_identifier"):
            return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    return "<anonymous>"


def _extract_docstring(node, source: bytes, lang: str) -> str | None:
    """Extract docstring/javadoc from a function/class node."""
    # Python: first child expression_statement with string
    if lang == "python":
        body = next((c for c in node.children if c.type == "block"), None)
        if body and body.children:
            first = body.children[0]
            if first.type == "expression_statement":
                expr = first.children[0] if first.children else None
                if expr and expr.type == "string":
                    return source[expr.start_byte:expr.end_byte].decode("utf-8", errors="replace")
    # Java: preceding comment
    prev = node.prev_named_sibling
    if prev and prev.type in ("comment", "block_comment"):
        return source[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
    return None


def chunk_file(file_path: str | Path) -> list[Chunk]:
    """Parse a source file and extract code chunks using tree-sitter."""
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return []

    suffix = path.suffix.lower()
    if suffix not in LANGUAGES:
        return []

    source = path.read_bytes()
    language = LANGUAGES[suffix]
    lang_name = LANGUAGE_NAMES[suffix]
    node_types = CHUNK_NODE_TYPES.get(suffix, set())

    parser = Parser(language)
    tree = parser.parse(source)

    chunks = []
    last_modified = datetime.fromtimestamp(path.stat().st_mtime)

    def walk(node, depth=0):
        if node.type in node_types:
            content = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            name = _get_node_name(node, source)
            docstring = _extract_docstring(node, source, lang_name)

            # Determine chunk type
            chunk_type = "function"
            if "class" in node.type:
                chunk_type = "class"
            elif "method" in node.type or "constructor" in node.type:
                chunk_type = "method"

            chunks.append(Chunk(
                content=content,
                file_path=str(path),
                language=lang_name,
                chunk_type=chunk_type,
                name=name,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                last_modified=last_modified,
                docstring=docstring,
            ))

        for child in node.children:
            walk(child, depth + 1)

    walk(tree.root_node)

    # If no chunks found (e.g., script with no functions), chunk the whole file
    if not chunks and len(source) < 10000:
        content = source.decode("utf-8", errors="replace")
        chunks.append(Chunk(
            content=content,
            file_path=str(path),
            language=lang_name,
            chunk_type="module",
            name=path.stem,
            start_line=1,
            end_line=content.count("\n") + 1,
            last_modified=last_modified,
        ))

    return chunks


def chunk_directory(directory: str | Path, extensions: set[str] | None = None) -> list[Chunk]:
    """Recursively chunk all supported source files in a directory."""
    root = Path(directory)
    if extensions is None:
        extensions = set(LANGUAGES.keys())

    all_chunks = []
    for path in root.rglob("*"):
        if path.suffix.lower() in extensions and not any(
            p in str(path) for p in ["/node_modules/", "/.git/", "/target/", "/__pycache__/", "/tmp-m2-repo/", "/.venv/"]
        ):
            all_chunks.extend(chunk_file(path))

    return all_chunks
