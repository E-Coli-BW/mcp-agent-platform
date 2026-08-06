"""Canonical source metadata shared by every chunker."""
from __future__ import annotations

import hashlib
from pathlib import Path


def enrich_chunks(chunks: list, *, tenant_id: str = "default") -> list:
    """Attach stable source/parent/version metadata without changing content."""
    for index, chunk in enumerate(chunks):
        source_key = str(Path(chunk.file_path).resolve())
        document_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        section_key = hashlib.sha256(str(chunk.name).encode()).hexdigest()[:10]
        parent_id = f"{document_id}:section:{section_key}"
        version = str(int(chunk.last_modified.timestamp())) if chunk.last_modified else "unknown"
        chunk.metadata = {
            **(chunk.metadata or {}),
            "document_id": document_id,
            "document_parent_id": f"{document_id}:document",
            "parent_id": parent_id,
            "chunk_id": f"{document_id}:{index}",
            "source_uri": source_key,
            "source_type": Path(chunk.file_path).suffix.lower().lstrip(".") or "text",
            "section_path": chunk.name,
            "tenant_id": tenant_id,
            "version": version,
            "acl": ["*"],
        }
    return chunks
