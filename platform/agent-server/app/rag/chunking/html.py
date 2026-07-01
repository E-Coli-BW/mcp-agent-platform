"""HTML chunker that splits documents by heading tags."""

import html
import re
from datetime import datetime
from pathlib import Path

from app.rag.chunking.code import Chunk
from app.rag.chunking.fixed_size import chunk_fixed_size

HEADING_PATTERN = re.compile(r"(?is)<h([1-3])[^>]*>(.*?)</h\1>")
TAG_PATTERN = re.compile(r"<[^>]+>")


def chunk_html(file_path: Path) -> list[Chunk]:
    """Split HTML documents by headings and strip markup from chunk content."""
    if not file_path.exists() or not file_path.is_file():
        return []

    content = file_path.read_text(encoding="utf-8", errors="replace")
    matches = list(HEADING_PATTERN.finditer(content))
    if not matches:
        return chunk_fixed_size(file_path)

    last_modified = datetime.fromtimestamp(file_path.stat().st_mtime)
    chunks: list[Chunk] = []

    for index, match in enumerate(matches):
        start = 0 if index == 0 and match.start() > 0 else match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        section_html = content[start:end]
        section_text = html.unescape(TAG_PATTERN.sub("", section_html)).strip()
        if not section_text:
            continue

        start_line = content.count("\n", 0, start) + 1
        end_line = start_line + section_text.count("\n")
        heading_text = html.unescape(TAG_PATTERN.sub("", match.group(2))).strip()
        chunks.append(
            Chunk(
                content=section_text,
                file_path=str(file_path),
                language="html",
                chunk_type="section",
                name=heading_text or file_path.stem,
                start_line=start_line,
                end_line=end_line,
                last_modified=last_modified,
            )
        )

    return chunks
