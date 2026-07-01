"""Embedding — converts text to dense vectors via Ollama.

Public entry points:
    from app.rag.embedding import embed_text, embed_texts, MAX_EMBED_CHARS
"""

from app.rag.embedding.embedder import (
    MAX_EMBED_CHARS,
    embed_text,
    embed_texts,
)

__all__ = ["embed_text", "embed_texts", "MAX_EMBED_CHARS"]
