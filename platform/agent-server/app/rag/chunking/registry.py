"""Registry that dispatches to the right chunker based on file extension."""

from pathlib import Path

from app.rag.chunking.code import Chunk, chunk_file as tree_sitter_chunk

CHUNKER_MAP: dict[str, str] = {
    ".py": "tree_sitter",
    ".java": "tree_sitter",
    ".js": "tree_sitter",
    ".ts": "tree_sitter",
    ".tsx": "tree_sitter",
    ".md": "markdown",
    ".yaml": "openapi_or_yaml",
    ".yml": "openapi_or_yaml",
    ".json": "openapi_or_json",
    ".pdf": "pdf",
    ".html": "html",
    ".txt": "fixed_size",
    ".csv": "fixed_size",
}


def chunk_file(file_path: str | Path) -> list[Chunk]:
    """Dispatch to the appropriate chunker based on file extension."""
    path = Path(file_path)
    ext = path.suffix.lower()
    chunker_type = CHUNKER_MAP.get(ext)

    if chunker_type == "tree_sitter" or chunker_type is None:
        return tree_sitter_chunk(str(path))
    if chunker_type == "markdown":
        from app.rag.chunking.markdown import chunk_markdown

        return chunk_markdown(path)
    if chunker_type in ("openapi_or_yaml", "openapi_or_json"):
        from app.rag.chunking.openapi import chunk_openapi_or_fallback

        return chunk_openapi_or_fallback(path)
    if chunker_type == "pdf":
        from app.rag.chunking.pdf import chunk_pdf

        return chunk_pdf(path)
    if chunker_type == "fixed_size":
        from app.rag.chunking.fixed_size import chunk_fixed_size

        return chunk_fixed_size(path)
    if chunker_type == "html":
        from app.rag.chunking.html import chunk_html

        return chunk_html(path)
    return tree_sitter_chunk(str(path))


def chunk_directory(directory: str | Path, extensions: set[str] | None = None) -> list[Chunk]:
    """Recursively chunk all supported files in a directory."""
    root = Path(directory)
    if extensions is None:
        extensions = set(CHUNKER_MAP.keys())

    all_chunks: list[Chunk] = []
    skip_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "target",
        "dist",
        "build",
        "tmp-m2-repo",
    }

    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions and not any(
            part in skip_dirs for part in path.parts
        ):
            all_chunks.extend(chunk_file(path))

    return all_chunks
