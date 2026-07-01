"""CLI to index a codebase into the RAG pipeline.

Usage:
    python -m app.rag.index.indexer /path/to/codebase
    python -m app.rag.index.indexer --search "query"

Indexes are stored per-workspace under ~/.mcp-local/rag-index/<hash>/ or
per-tenant under ~/.mcp-local/rag-index/<tenant>/<collection>/.
"""

import argparse
import asyncio
import hashlib
import os
import time
from pathlib import Path

from app.rag.chunking.registry import chunk_directory
from app.rag.embedding.embedder import MAX_EMBED_CHARS, embed_texts
from app.rag.index.retriever import get_retriever

BASE_INDEX_DIR = Path.home() / ".mcp-local" / "rag-index"
DEFAULT_COLLECTION = "default"
SUPPORTED_FILE_TYPES = ".py, .java, .js, .ts, .md, .yaml, .json, .pdf, .html, .txt"


def _workspace_index_dir(workspace_path: str) -> Path:
    """Get per-workspace index directory. Uses hash of absolute path."""
    real = os.path.realpath(os.path.expanduser(workspace_path))
    workspace_hash = hashlib.sha256(real.encode()).hexdigest()[:12]
    name = Path(real).name
    return BASE_INDEX_DIR / f"{name}-{workspace_hash}"


def get_index_dir_for_workspace(workspace_path: str | None = None) -> Path:
    """Get index directory — per-workspace if path given, else legacy global."""
    if workspace_path:
        return _workspace_index_dir(workspace_path)
    return BASE_INDEX_DIR


def get_index_dir_for_tenant(
    tenant_id: str,
    collection: str = DEFAULT_COLLECTION,
) -> Path:
    """Get the tenant-specific index directory."""
    return BASE_INDEX_DIR / tenant_id / collection


async def index_codebase(
    directory: str,
    tenant: str | None = None,
    collection: str = DEFAULT_COLLECTION,
) -> dict:
    """Index a codebase: chunk → embed → store in memory + disk."""
    index_dir = (
        get_index_dir_for_tenant(tenant, collection)
        if tenant
        else get_index_dir_for_workspace(directory)
    )
    print(f"📂 Indexing: {directory}")
    print(f"   Index dir: {index_dir}")
    start = time.time()

    print("  1. Chunking supported files...")
    chunks = chunk_directory(directory)
    print(f"     Found {len(chunks)} chunks from {len(set(c.file_path for c in chunks))} files")

    if not chunks:
        print("  ❌ No supported files found")
        return {"chunks": 0}

    print(f"  2. Embedding {len(chunks)} chunks (batch size=10)...")
    texts = []
    for chunk in chunks:
        text = chunk.content[:MAX_EMBED_CHARS]
        if chunk.name:
            text = f"{chunk.name}: {text}"
        texts.append(text)

    embeddings = await embed_texts(texts, batch_size=10)

    failures = sum(1 for embedding in embeddings if all(value == 0.0 for value in embedding[:10]))
    if failures > 0:
        print(f"     ⚠️ {failures}/{len(chunks)} chunks failed embedding (zero vector)")
    else:
        print(f"     ✅ All {len(chunks)} chunks embedded successfully")

    print("  3. Building search index...")
    retriever = get_retriever()
    retriever.load_index(chunks, embeddings)

    print(f"  4. Saving index to {index_dir}...")
    retriever.save_index(str(index_dir))

    elapsed = time.time() - start
    stats = {
        "chunks": len(chunks),
        "files": len(set(c.file_path for c in chunks)),
        "languages": list(set(c.language for c in chunks)),
        "elapsed_seconds": round(elapsed, 1),
    }
    print(f"  ✅ Done! {stats['chunks']} chunks, {stats['files']} files in {stats['elapsed_seconds']}s")
    return stats


async def test_search(
    query: str,
    workspace: str | None = None,
    top_k: int = 5,
    tenant: str | None = None,
    collection: str = DEFAULT_COLLECTION,
):
    """Test search against the indexed codebase."""
    retriever = get_retriever()
    if not retriever.is_indexed:
        if tenant:
            tenant_index = get_index_dir_for_tenant(tenant, collection)
            if retriever.load_from_disk(str(tenant_index)):
                pass
            elif workspace and retriever.load_from_disk(str(get_index_dir_for_workspace(workspace))):
                pass
            elif not retriever.load_from_disk(str(BASE_INDEX_DIR)):
                print("❌ No index found. Run: python -m app.rag.index.indexer /path/to/codebase")
                return
        else:
            idx = get_index_dir_for_workspace(workspace)
            if not retriever.load_from_disk(str(idx)):
                if not retriever.load_from_disk(str(BASE_INDEX_DIR)):
                    print("❌ No index found. Run: python -m app.rag.index.indexer /path/to/codebase")
                    return

    print(f"\n🔍 Searching: \"{query}\"")
    results = await retriever.search(query, top_k=top_k)
    for i, result in enumerate(results):
        fname = Path(result.file_path).name
        print(
            f"  {i + 1}. [{result.score:.4f}] {fname}:{result.start_line}-{result.end_line} "
            f"({result.chunk_type} {result.name})"
        )
        preview = result.content[:100].replace("\n", " ")
        print(f"     {preview}...")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index and search RAG chunks")
    parser.add_argument("directory", nargs="?", help="Workspace directory to index")
    parser.add_argument("--search", dest="search_query", help="Search the stored index")
    parser.add_argument("--tenant", help="Tenant ID for tenant-scoped indexes")
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help="Collection name for tenant-scoped indexes",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    print(f"Supported: {SUPPORTED_FILE_TYPES}")

    if args.search_query:
        asyncio.run(
            test_search(
                args.search_query,
                workspace=args.directory,
                tenant=args.tenant,
                collection=args.collection,
            )
        )
    elif args.directory:
        asyncio.run(index_codebase(args.directory, tenant=args.tenant, collection=args.collection))
        asyncio.run(
            test_search(
                "authentication JWT filter",
                workspace=args.directory,
                tenant=args.tenant,
                collection=args.collection,
            )
        )
    else:
        parser.print_help()
        raise SystemExit(1)
