# RAG Pipeline — Design Document

> For the Coding Agent Server. Focused on **code** retrieval, not general text.

---

## 1. Why RAG for a Coding Agent?

Without RAG, the agent can only:
- Ask the LLM to generate from training data (stale, generic)
- Use `file_search` (grep — keyword only, no semantic understanding)
- Read entire files (wastes tokens on irrelevant code)

With RAG:
- Agent retrieves **relevant code chunks** before generating
- LLM sees real project code, not hallucinated patterns
- Search is **semantic** ("authentication flow" finds `JwtAuthFilter` even without keyword match)

---

## 2. Pipeline Architecture

```
Indexing (offline, once per project):
  Codebase files
    → tree-sitter AST parsing (function/class boundaries)
    → Semantic chunks (each function = 1 chunk)
    → Embed chunks (nomic-embed-text via Ollama)
    → Store in pgvector + BM25 index (PostgreSQL)

Query (online, per user request):
  User query: "how does authentication work?"
    → Embed query
    → Vector search (top 50 by cosine similarity)
    → BM25 keyword search (top 50, parallel)
    → Reciprocal Rank Fusion (merge both lists)
    → Re-rank with cross-encoder (top 50 → top 10)
    → Metadata filter (language, recency, proximity)
    → Return top 10 chunks with source attribution
```

---

## 3. Code Chunking Strategy

### Why Not Fixed-Size Chunks?

```python
# BAD: 512-token fixed chunks split functions in half
chunk_1 = "def authenticate(user, password):\n    if not user:\n        raise"
chunk_2 = "ValueError('no user')\n    token = jwt.encode({...})\n    return token"
# LLM sees broken code → bad retrieval quality
```

### Semantic Chunking with tree-sitter

```python
# GOOD: Each function/class/method = one chunk
import tree_sitter_languages

parser = tree_sitter_languages.get_parser("python")
tree = parser.parse(source_code.encode())

# Walk AST → extract functions, classes, methods
for node in walk_tree(tree.root_node):
    if node.type in ("function_definition", "class_definition", "method_definition"):
        chunk = Chunk(
            content=source_code[node.start_byte:node.end_byte],
            metadata={
                "file": file_path,
                "language": "python",
                "type": node.type,           # function, class, method
                "name": get_name(node),       # "authenticate"
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
            }
        )
```

### Chunk Metadata (Critical for Filtering)

```python
@dataclass
class ChunkMetadata:
    file_path: str          # src/auth/jwt_filter.py
    language: str           # python, java, typescript
    chunk_type: str         # function, class, method, module-level
    name: str               # "authenticate", "JwtAuthFilter"
    start_line: int         # 42
    end_line: int           # 67
    last_modified: datetime  # file modification time
    dependencies: list[str]  # imports used by this chunk
    docstring: str | None   # extracted docstring
```

---

## 4. Embedding & Storage

### Embedding Model

| Model | Dims | Speed | Quality | Cost |
|-------|------|-------|---------|------|
| `nomic-embed-text` (Ollama) | 768 | Fast | Good | **Free (local)** |
| `mxbai-embed-large` (Ollama) | 1024 | Medium | Better | **Free (local)** |
| `text-embedding-3-small` (OpenAI) | 1536 | Fast | Great | $0.02/1M tokens |

**MVP choice:** `mxbai-embed-large` via Ollama — already installed locally, free, good quality.

### Storage: pgvector (PostgreSQL Extension)

Reuse existing PostgreSQL from memory-server's docker-compose:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE code_chunks (
    id          SERIAL PRIMARY KEY,
    file_path   TEXT NOT NULL,
    language    VARCHAR(20),
    chunk_type  VARCHAR(30),     -- function, class, method
    name        VARCHAR(256),     -- function/class name
    content     TEXT NOT NULL,
    start_line  INTEGER,
    end_line    INTEGER,
    embedding   vector(1024),     -- mxbai-embed-large dimensions
    metadata    JSONB,
    indexed_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Vector similarity search index
CREATE INDEX idx_chunks_embedding ON code_chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- BM25-like keyword search
ALTER TABLE code_chunks ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content || ' ' || COALESCE(name, ''))) STORED;
CREATE INDEX idx_chunks_tsv ON code_chunks USING gin(tsv);

-- Metadata filtering
CREATE INDEX idx_chunks_language ON code_chunks(language);
CREATE INDEX idx_chunks_file ON code_chunks(file_path);
```

---

## 5. Retrieval Pipeline

### Step 1: Hybrid Search (Vector + BM25)

```python
async def hybrid_search(query: str, top_k: int = 50) -> list[ScoredChunk]:
    query_embedding = await embed(query)

    # Vector search (semantic)
    vector_results = await db.execute("""
        SELECT id, content, metadata,
               1 - (embedding <=> $1) AS score
        FROM code_chunks
        ORDER BY embedding <=> $1
        LIMIT $2
    """, query_embedding, top_k)

    # BM25 keyword search (exact match)
    bm25_results = await db.execute("""
        SELECT id, content, metadata,
               ts_rank_cd(tsv, plainto_tsquery('english', $1)) AS score
        FROM code_chunks
        WHERE tsv @@ plainto_tsquery('english', $1)
        ORDER BY score DESC
        LIMIT $2
    """, query, top_k)

    return reciprocal_rank_fusion(vector_results, bm25_results)
```

### Step 2: Reciprocal Rank Fusion (RRF)

```python
def reciprocal_rank_fusion(
    *result_lists: list[ScoredChunk], k: int = 60
) -> list[ScoredChunk]:
    """Merge multiple ranked lists into one using RRF.

    RRF score = sum(1 / (k + rank_i)) for each list
    This is better than raw score merging because different
    search methods have incomparable score scales.
    """
    scores: dict[str, float] = {}
    chunks: dict[str, ScoredChunk] = {}

    for results in result_lists:
        for rank, chunk in enumerate(results):
            chunk_id = chunk.id
            scores[chunk_id] = scores.get(chunk_id, 0) + 1.0 / (k + rank + 1)
            chunks[chunk_id] = chunk

    sorted_ids = sorted(scores, key=scores.get, reverse=True)
    return [chunks[cid] for cid in sorted_ids]
```

### Step 3: Cross-Encoder Re-ranking (Optional, High Quality)

```python
from sentence_transformers import CrossEncoder

reranker = CrossEncoder("BAAI/bge-reranker-base")

def rerank(query: str, chunks: list[ScoredChunk], top_k: int = 10) -> list[ScoredChunk]:
    """Re-rank candidates with cross-encoder for higher precision."""
    pairs = [(query, chunk.content) for chunk in chunks]
    scores = reranker.predict(pairs)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
    return [chunk for chunk, score in ranked[:top_k]]
```

### Step 4: Metadata Filtering

```python
def filter_by_context(chunks: list[ScoredChunk], context: QueryContext) -> list[ScoredChunk]:
    """Boost/filter based on user context."""
    for chunk in chunks:
        # Boost same language as user's current file
        if chunk.language == context.current_language:
            chunk.score *= 1.5
        # Boost files near user's current file
        if is_nearby(chunk.file_path, context.current_file):
            chunk.score *= 1.3
        # Boost recently modified files
        if chunk.last_modified > context.recent_threshold:
            chunk.score *= 1.2
    return sorted(chunks, key=lambda c: c.score, reverse=True)
```

---

## 6. Evaluation

### Offline: Gold Standard Dataset

```python
# Build 50-100 query → expected_files pairs from our own codebase
gold_standard = [
    {"query": "JWT authentication filter",
     "expected": ["JwtAuthFilter.java", "SecurityConfig.java"]},
    {"query": "how memory search works",
     "expected": ["MemorySearchEngine.java", "MemoryService.java"]},
    {"query": "process sandbox timeout",
     "expected": ["ProcessSandbox.java"]},
]

# Metrics
for item in gold_standard:
    retrieved = rag.search(item["query"], top_k=10)
    recall = len(set(retrieved_files) & set(item["expected"])) / len(item["expected"])
    mrr = 1 / (first_relevant_rank + 1)
```

### Online: Implicit Signals

```python
# Log every retrieval
retrieval_log.append({
    "query": query,
    "chunks_returned": [c.id for c in chunks],
    "chunks_used_by_agent": [c.id for c in chunks if c.id in agent_response],
    "user_accepted": user_gave_thumbs_up,
})
```

---

## 7. Implementation Plan

```
app/rag/
├── chunking/
│   ├── code.py            — tree-sitter code chunker (Chunk, CodeChunk, chunk_file)
│   ├── registry.py        — file-extension dispatcher (chunk_directory)
│   ├── fixed_size.py      — line-window fallback chunker
│   ├── html.py            — heading-based HTML splitter
│   ├── markdown.py        — heading-based markdown splitter
│   ├── openapi.py         — OpenAPI/YAML/JSON-aware chunker
│   └── pdf.py             — PDF page-based chunker
├── embedding/
│   └── embedder.py        — Ollama embedding client (mxbai-embed-large)
├── index/
│   ├── retriever.py       — VectorRetriever SPI + InMemoryRetriever (numpy)
│   ├── redis_retriever.py — production backend (RediSearch HNSW)
│   └── indexer.py         — CLI + library: bulk-index a workspace
├── reranking/
│   ├── dispatcher.py      — strategy router (none|heuristic|cross_encoder|llm|auto)
│   ├── cross_encoder.py   — MiniLM pairwise reranker (~100ms)
│   ├── llm.py             — listwise LLM reranker (~500ms-1s)
│   └── learned.py         — online-learned reranker (logistic regression)
├── compression/
│   └── ast.py             — AST-aware code compression for tool output
└── eval/
    ├── dataset.py         — BENCHMARK pairs + recall@K runner
    ├── benchmark_rerankers.py
    └── benchmark_compression.py

Dependencies:
  pip install tree-sitter tree-sitter-languages
  pip install sentence-transformers  (for cross-encoder reranker)
  pip install psycopg2-binary        (for pgvector — not currently used)
  pip install pgvector               (vector type support — not currently used)

Database:
  PostgreSQL with pgvector extension (reuse docker-compose)

Embedding:
  Ollama mxbai-embed-large (already installed locally)
```

---

## 8. Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Chunking | tree-sitter AST | Preserves code structure, function boundaries |
| Embedding | Ollama local (mxbai-embed-large) | Free, no API key, good for code |
| Vector DB | pgvector | Reuse existing PG, no new infra |
| Keyword search | PostgreSQL tsvector | BM25-equivalent, already in PG |
| Merge strategy | Reciprocal Rank Fusion | Scale-invariant, proven effective |
| Re-ranking | Cross-encoder (optional) | Expensive but high quality for top-K |
| Metadata | Language, recency, proximity | Critical for code relevance |

---

## 9. What We DON'T Build Yet

| Feature | Why Defer |
|---------|-----------|
| Incremental indexing (file watcher) | Full re-index is fast enough for MVP |
| Multi-repo support | Single repo is enough to prove the pipeline |
| Embedding fine-tuning | Off-the-shelf models are good enough |
| Semantic caching | Not needed until we hit cost issues |
| Graph-based code understanding | tree-sitter AST is sufficient for chunking |
