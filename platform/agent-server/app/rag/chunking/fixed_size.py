"""Fixed-size text chunker for plain-text documents."""

from datetime import datetime
from pathlib import Path

from app.rag.chunking.code import Chunk


def chunk_fixed_size(file_path: Path, max_lines: int = 50, overlap: int = 5) -> list[Chunk]:
    """Split a file into overlapping line-based chunks."""
    if not file_path.exists() or not file_path.is_file():
        return []

    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines() or [""]
    language = file_path.suffix.lower().lstrip(".") or "text"
    last_modified = datetime.fromtimestamp(file_path.stat().st_mtime)

    if len(lines) <= max_lines:
        return [
            Chunk(
                content=content,
                file_path=str(file_path),
                language=language,
                chunk_type="text_block",
                name=f"{file_path.name}:1-{len(lines)}",
                start_line=1,
                end_line=len(lines),
                last_modified=last_modified,
            )
        ]

    step = max(max_lines - overlap, 1)
    chunks: list[Chunk] = []

    for start in range(0, len(lines), step):
        end = min(start + max_lines, len(lines))
        chunk_lines = lines[start:end]
        chunks.append(
            Chunk(
                content="\n".join(chunk_lines),
                file_path=str(file_path),
                language=language,
                chunk_type="text_block",
                name=f"{file_path.name}:{start + 1}-{end}",
                start_line=start + 1,
                end_line=end,
                last_modified=last_modified,
            )
        )
        if end >= len(lines):
            break

    return chunks
