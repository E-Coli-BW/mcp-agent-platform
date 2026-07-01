"""Markdown chunker that splits documents by headings."""

import re
from datetime import datetime
from pathlib import Path

from app.rag.chunking.code import Chunk

HEADING_PATTERN = r"^(#{1,3}\s+.+)$"


def chunk_markdown(file_path: Path) -> list[Chunk]:
    """Split a Markdown file into heading-based sections."""
    if not file_path.exists() or not file_path.is_file():
        return []

    content = file_path.read_text(encoding="utf-8", errors="replace")
    last_modified = datetime.fromtimestamp(file_path.stat().st_mtime)
    parts = re.split(HEADING_PATTERN, content, flags=re.MULTILINE)
    heading_matches = list(re.finditer(HEADING_PATTERN, content, flags=re.MULTILINE))

    if len(parts) <= 1 or not heading_matches:
        end_line = max(content.count("\n") + 1, 1)
        return [
            Chunk(
                content=content,
                file_path=str(file_path),
                language="markdown",
                chunk_type="document",
                name=file_path.stem,
                start_line=1,
                end_line=end_line,
                last_modified=last_modified,
            )
        ]

    chunks: list[Chunk] = []
    first_section_start = 0 if parts[0] else heading_matches[0].start()

    for index, match in enumerate(heading_matches):
        start = first_section_start if index == 0 else match.start()
        end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(content)
        section_content = content[start:end].strip()
        if not section_content:
            continue

        start_line = content.count("\n", 0, start) + 1
        end_line = start_line + section_content.count("\n")
        chunks.append(
            Chunk(
                content=section_content,
                file_path=str(file_path),
                language="markdown",
                chunk_type="section",
                name=match.group(1).lstrip("#").strip(),
                start_line=start_line,
                end_line=end_line,
                last_modified=last_modified,
            )
        )

    return chunks
