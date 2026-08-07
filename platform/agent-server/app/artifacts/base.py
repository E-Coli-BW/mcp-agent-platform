"""Contracts for deterministic artifacts produced from tool evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

_ARTIFACT_PREFIX = "<<<VERIFIED_ARTIFACT "
_ARTIFACT_SUFFIX = ">>>"
_ARTIFACT_END = "<<<END_VERIFIED_ARTIFACT>>>"


@dataclass(frozen=True)
class VerifiedArtifact:
    """A deterministic, plugin-owned artifact safe to bypass LLM rewriting."""

    plugin_id: str
    artifact_type: str
    source_path: str
    content: str

    def to_tool_block(self) -> str:
        metadata = json.dumps(
            {
                "p": self.plugin_id,
                "t": self.artifact_type,
                "s": self.source_path,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return (
            f"{_ARTIFACT_PREFIX}{metadata}{_ARTIFACT_SUFFIX}\n"
            f"{self.content.rstrip()}\n{_ARTIFACT_END}"
        )

    @classmethod
    def from_tool_output(cls, output: str) -> VerifiedArtifact | None:
        if not output.startswith(_ARTIFACT_PREFIX):
            return None
        start = 0
        header_end = output.find(_ARTIFACT_SUFFIX, start + len(_ARTIFACT_PREFIX))
        if header_end < 0:
            return None
        content_start = header_end + len(_ARTIFACT_SUFFIX)
        if output[content_start : content_start + 1] == "\n":
            content_start += 1
        content_end = output.find(_ARTIFACT_END, content_start)
        if content_end < 0:
            return None

        try:
            metadata = json.loads(output[start + len(_ARTIFACT_PREFIX) : header_end])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(metadata, dict):
            return None

        plugin_id = metadata.get("p")
        artifact_type = metadata.get("t")
        source_path = metadata.get("s")
        if not all(
            isinstance(value, str) and value for value in (plugin_id, artifact_type, source_path)
        ):
            return None
        content = output[content_start:content_end].rstrip()
        if not content:
            return None
        return cls(
            plugin_id=plugin_id,
            artifact_type=artifact_type,
            source_path=source_path,
            content=content,
        )


class ArtifactPlugin(Protocol):
    """Static plugin interface embedded at the tool/graph boundary."""

    plugin_id: str

    def extract_file(
        self,
        *,
        path: str,
        full_text: str,
        selected_text: str,
    ) -> VerifiedArtifact | None: ...

    def accepts_request(self, prompt: str, artifact: VerifiedArtifact) -> bool: ...

    def required_tools(
        self,
        prompt: str,
        tool_outputs: Iterable[str],
    ) -> tuple[str, ...]: ...

    def validate(self, artifact: VerifiedArtifact) -> bool: ...

    def render_answer(self, artifact: VerifiedArtifact) -> str: ...
