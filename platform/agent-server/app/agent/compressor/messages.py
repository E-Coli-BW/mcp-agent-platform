"""Model-view message budgets and compression policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.agent.compressor.investigation import InvestigationState, _extract_skill_key

logger = logging.getLogger(__name__)

_RECENT_TOOL_OUTPUT_MAX_CHARS = 1_200


@dataclass(frozen=True)
class CompressionResult:
    messages: list[BaseMessage]
    summary: str
    overflow: bool
    used_chars: int
    budget_chars: int

    def __iter__(self):
        yield self.messages
        yield self.summary


def estimate_chars(messages: list[BaseMessage] | str) -> int:
    if isinstance(messages, str):
        return len(messages)
    total = 0
    for message in messages:
        content = (
            message.content
            if isinstance(message.content, str)
            else str(message.content)
        )
        total += len(content)
        if isinstance(message, AIMessage) and message.tool_calls:
            total += len(str(message.tool_calls))
    return total


def estimate_tokens(messages: list[BaseMessage] | str) -> int:
    return estimate_chars(messages) // 4


def compress_messages(
    messages: list[BaseMessage],
    budget_chars: int,
    investigation: InvestigationState | None = None,
) -> CompressionResult:
    """Build a compressed model view while retaining goal, facts, and recent turns."""
    budget_chars = max(0, budget_chars)
    if not messages:
        return CompressionResult(messages, "", False, 0, budget_chars)

    current_chars = estimate_chars(messages)
    if current_chars <= budget_chars:
        return CompressionResult(
            messages,
            "",
            False,
            current_chars,
            budget_chars,
        )

    first_human_index = next(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, HumanMessage)
        ),
        0,
    )
    head_end = first_human_index + 1
    tail_start = max(head_end, len(messages) - 6)
    head = messages[:head_end]
    middle = messages[head_end:tail_start]
    tail = messages[tail_start:]

    compressed_middle = []
    summary_lines = []
    for message in middle:
        compressed, summary = _compress_middle_message(message)
        if compressed is not None:
            compressed_middle.append(compressed)
        if summary:
            summary_lines.append(summary)

    facts_messages = _investigation_messages(investigation)
    result = head + facts_messages + compressed_middle + tail
    if estimate_chars(result) > budget_chars:
        compacted_tail = []
        compacted_recent = 0
        for message in tail:
            if isinstance(message, ToolMessage):
                content = (
                    message.content
                    if isinstance(message.content, str)
                    else str(message.content)
                )
                if len(content) > _RECENT_TOOL_OUTPUT_MAX_CHARS:
                    omitted = len(content) - _RECENT_TOOL_OUTPUT_MAX_CHARS
                    message = ToolMessage(
                        id=message.id,
                        content=(
                            content[:_RECENT_TOOL_OUTPUT_MAX_CHARS]
                            + f"\n[...{omitted} recent tool-output chars truncated]"
                        ),
                        tool_call_id=message.tool_call_id,
                        name=message.name,
                        status=getattr(message, "status", None),
                    )
                    compacted_recent += 1
            compacted_tail.append(message)
        if compacted_recent:
            summary_lines.append(
                f"[compacted {compacted_recent} recent tool output(s)]"
            )
            tail = compacted_tail
            result = head + facts_messages + compressed_middle + tail
    while estimate_chars(result) > budget_chars and compressed_middle:
        dropped = compressed_middle.pop(0)
        content = (
            dropped.content
            if isinstance(dropped.content, str)
            else str(dropped.content)
        )
        summary_lines.append(
            f"[dropped {type(dropped).__name__}: {content[:50]}...]"
        )
        result = head + facts_messages + compressed_middle + tail

    used_chars = estimate_chars(result)
    overflow = used_chars > budget_chars
    logger.info(
        "Compression: %d -> %d chars (%.0f%% reduction), "
        "%d items summarized, overflow=%s",
        current_chars,
        used_chars,
        (1 - used_chars / current_chars) * 100 if current_chars > 0 else 0,
        len(summary_lines),
        overflow,
    )
    return CompressionResult(
        messages=result,
        summary="\n".join(summary_lines),
        overflow=overflow,
        used_chars=used_chars,
        budget_chars=budget_chars,
    )


def _compress_middle_message(
    message: BaseMessage,
) -> tuple[BaseMessage | None, str | None]:
    content = (
        message.content if isinstance(message.content, str) else str(message.content)
    )
    if isinstance(message, ToolMessage):
        if "[SKILL ACTIVATED:" in content:
            key = _extract_skill_key(content)
            return (
                ToolMessage(
                    id=message.id,
                    content=f"[Applied skill:{key} — see skill_get for details]",
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                ),
                None,
            )
        if content.startswith("❌") or getattr(message, "status", None) == "error":
            first_line = content.splitlines()[0][:150]
            return (
                ToolMessage(
                    id=message.id,
                    content=first_line,
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                ),
                f"Tool {message.name} failed: {first_line[:80]}",
            )
        if len(content) > 500:
            return (
                ToolMessage(
                    id=message.id,
                    content=(
                        content[:200]
                        + f"\n[...{len(content) - 200} chars truncated]"
                    ),
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                ),
                None,
            )
        return message, None

    if isinstance(message, AIMessage) and not message.tool_calls and len(content) > 300:
        return (
            AIMessage(id=message.id, content=content[:200] + "...[truncated]"),
            f"AI explained: {content[:80]}...",
        )
    return message, None


def _investigation_messages(
    investigation: InvestigationState | None,
) -> list[BaseMessage]:
    if not investigation or not investigation.confirmed_facts:
        return []
    return [
        HumanMessage(
            id="investigation-state-view",
            content=(
                "[INVESTIGATION STATE — retained from earlier turns]\n"
                + investigation.to_summary_block()
            ),
        )
    ]


CompressionResult.__module__ = "app.agent.compressor"
