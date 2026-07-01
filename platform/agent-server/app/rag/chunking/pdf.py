"""PDF chunker with graceful degradation when PyPDF2 is unavailable."""

from datetime import datetime
from pathlib import Path

from app.rag.chunking.code import Chunk


def chunk_pdf(file_path: Path) -> list[Chunk]:
    """Extract one chunk per PDF page with text content."""
    if not file_path.exists() or not file_path.is_file():
        return []

    last_modified = datetime.fromtimestamp(file_path.stat().st_mtime)
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return [
            Chunk(
                content="PDF reading requires PyPDF2: pip install PyPDF2",
                file_path=str(file_path),
                language="pdf",
                chunk_type="document",
                name=file_path.name,
                start_line=1,
                end_line=1,
                last_modified=last_modified,
            )
        ]

    reader = PdfReader(str(file_path))
    chunks: list[Chunk] = []
    current_line = 1

    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if not text.strip():
            continue
        line_count = max(text.count("\n") + 1, 1)
        chunks.append(
            Chunk(
                content=text,
                file_path=str(file_path),
                language="pdf",
                chunk_type="page",
                name=f"Page {index + 1}",
                start_line=current_line,
                end_line=current_line + line_count - 1,
                last_modified=last_modified,
            )
        )
        current_line += line_count

    return chunks
