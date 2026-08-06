"""Context Compression — intelligent conversation history compression.

Implements a compressor that fires when the context window exceeds a budget threshold.
Uses a tiered strategy:
  1. Structured fact extraction → InvestigationState (never lost)
  2. Skill execution traces → compressed to references (recoverable via skill_get)
  3. Older tool outputs → aggressive summarization
  4. Failed attempts → single-line summaries
  5. Recent turns → kept verbatim

The compressor is a LangGraph node that conditionally fires between
track_errors and call_llm.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Investigation State (structured extraction) ───────────────────────────────


class InvestigationState(BaseModel):
    """Structured extraction of the current investigation.

    Updated after every meaningful tool call. Survives compression because
    it's stored as a dedicated state field, not in the message list.

    This gives the compressor a "ground truth" of what must be retained,
    making lossy compression safe.
    """

    goal: str = ""
    confirmed_facts: list[str] = Field(default_factory=list)
    current_hypothesis: str = ""
    eliminated: list[str] = Field(default_factory=list)  # "X because Y"
    key_artifacts: dict[str, str] = Field(default_factory=dict)  # filename → snippet
    skills_used: list[str] = Field(default_factory=list)  # skill keys applied
    next_steps: list[str] = Field(default_factory=list)

    def to_summary_block(self) -> str:
        """Render as a compact block for injection into system prompt after compression."""
        lines = []
        if self.goal:
            lines.append(f"**Goal**: {self.goal}")
        if self.confirmed_facts:
            lines.append("**Confirmed facts**:")
            for f in self.confirmed_facts[-10:]:  # cap at 10
                lines.append(f"  - {f}")
        if self.current_hypothesis:
            lines.append(f"**Current hypothesis**: {self.current_hypothesis}")
        if self.eliminated:
            lines.append("**Eliminated**:")
            for e in self.eliminated[-5:]:  # cap at 5
                lines.append(f"  - {e}")
        if self.key_artifacts:
            lines.append("**Key artifacts**:")
            for name, snippet in list(self.key_artifacts.items())[-5:]:
                lines.append(f"  - {name}: {snippet[:100]}")
        if self.skills_used:
            lines.append(f"**Skills used**: {', '.join(self.skills_used)}")
        if self.next_steps:
            lines.append("**Next steps**:")
            for s in self.next_steps[:3]:
                lines.append(f"  - {s}")
        return "\n".join(lines) if lines else ""

    def token_estimate(self) -> int:
        """Rough token estimate (4 chars ≈ 1 token)."""
        return len(self.to_summary_block()) // 4


@dataclass(frozen=True)
class CompressionResult:
    """Model-facing compressed view plus an explicit budget outcome."""

    messages: list[BaseMessage]
    summary: str
    overflow: bool
    used_chars: int
    budget_chars: int

    def __iter__(self):
        """Preserve tuple-unpacking compatibility for existing callers."""
        yield self.messages
        yield self.summary


# ── Compression Logic ─────────────────────────────────────────────────────────


def estimate_chars(messages: list[BaseMessage] | str) -> int:
    """Estimate prompt size in the configured character-budget unit."""
    if isinstance(messages, str):
        return len(messages)
    total = 0
    for message in messages:
        content = message.content if isinstance(message.content, str) else str(message.content)
        total += len(content)
        if isinstance(message, AIMessage) and message.tool_calls:
            total += len(str(message.tool_calls))
    return total


def estimate_tokens(messages: list[BaseMessage] | str) -> int:
    """Backward-compatible rough token estimate for metrics only."""
    return estimate_chars(messages) // 4


def compress_messages(
    messages: list[BaseMessage],
    budget_chars: int,
    investigation: InvestigationState | None = None,
) -> CompressionResult:
    """Compress message history to fit within budget.

    Returns:
        CompressionResult with the model view and explicit overflow status.

    Strategy:
        1. Always keep: first HumanMessage (original goal), last 6 messages
        2. Skill activations → one-line references
        3. Large tool outputs (>500 chars) older than last 6 → truncate to 200 chars
        4. Failed tool calls → "❌ [tool] failed: [first line of error]"
        5. Middle conversation turns → summarize to key points
    """
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

    # Split into: first message (goal), middle, recent tail
    first_human_idx = next(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), 0
    )
    # Keep the original goal and up to six recent messages as disjoint regions.
    head_end = first_human_idx + 1
    tail_start = max(head_end, len(messages) - 6)
    head = messages[:head_end]
    middle = messages[head_end:tail_start]
    tail = messages[tail_start:]

    # Compress middle section
    compressed_middle: list[BaseMessage] = []
    summary_lines: list[str] = []

    for msg in middle:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)

        if isinstance(msg, ToolMessage):
            # Skill activation → reference only
            if "[SKILL ACTIVATED:" in content:
                skill_key = _extract_skill_key(content)
                replacement = f"[Applied skill:{skill_key} — see skill_get for details]"
                compressed_middle.append(
                    ToolMessage(
                        id=msg.id,
                        content=replacement,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
                continue

            # Failed tool → one-line summary
            if content.startswith("❌") or getattr(msg, "status", None) == "error":
                first_line = content.split("\n")[0][:150]
                compressed_middle.append(
                    ToolMessage(
                        id=msg.id,
                        content=first_line,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
                summary_lines.append(f"Tool {msg.name} failed: {first_line[:80]}")
                continue

            # Large successful output → aggressive truncation
            if len(content) > 500:
                truncated = content[:200] + f"\n[...{len(content) - 200} chars truncated]"
                compressed_middle.append(
                    ToolMessage(
                        id=msg.id,
                        content=truncated,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
                continue

            # Small tool output → keep as-is
            compressed_middle.append(msg)

        elif isinstance(msg, AIMessage):
            # AI messages with tool calls → keep (they're structural)
            if msg.tool_calls:
                compressed_middle.append(msg)
            else:
                # AI text response → truncate if large
                if len(content) > 300:
                    compressed_middle.append(
                        AIMessage(id=msg.id, content=content[:200] + "...[truncated]")
                    )
                    summary_lines.append(f"AI explained: {content[:80]}...")
                else:
                    compressed_middle.append(msg)

        elif isinstance(msg, HumanMessage):
            # User follow-ups → keep (they're intent signals)
            compressed_middle.append(msg)
        else:
            compressed_middle.append(msg)

    # If we have investigation state, inject confirmed facts as a synthetic message
    # so the LLM retains critical info even after aggressive compression
    facts_messages: list[BaseMessage] = []
    if investigation and investigation.confirmed_facts:
        facts_block = (
            "[INVESTIGATION STATE — retained from earlier turns]\n"
            + investigation.to_summary_block()
        )
        facts_messages = [HumanMessage(id="investigation-state-view", content=facts_block)]

    # Build final compressed list. Facts are a dedicated component so every
    # degradation pass preserves them.
    result = head + facts_messages + compressed_middle + tail

    summary = "\n".join(summary_lines) if summary_lines else ""

    # If still over budget, drop oldest middle messages
    while estimate_chars(result) > budget_chars and compressed_middle:
        dropped = compressed_middle.pop(0)
        content = dropped.content if isinstance(dropped.content, str) else str(dropped.content)
        summary_lines.append(f"[dropped {type(dropped).__name__}: {content[:50]}...]")
        result = head + facts_messages + compressed_middle + tail
        summary = "\n".join(summary_lines)

    used_chars = estimate_chars(result)
    overflow = used_chars > budget_chars
    logger.info(
        "📦 Compression: %d → %d chars (%.0f%% reduction), %d items summarized, overflow=%s",
        current_chars,
        used_chars,
        (1 - used_chars / current_chars) * 100 if current_chars > 0 else 0,
        len(summary_lines),
        overflow,
    )

    return CompressionResult(
        messages=result,
        summary=summary,
        overflow=overflow,
        used_chars=used_chars,
        budget_chars=budget_chars,
    )


def _extract_skill_key(content: str) -> str:
    """Extract skill key from '[SKILL ACTIVATED: key-name]' block."""
    match = re.search(r"\[SKILL ACTIVATED:\s*([^\]]+)\]", content)
    return match.group(1).strip() if match else "unknown"


# ── Investigation State Updater ───────────────────────────────────────────────


def update_investigation_from_messages(
    state: InvestigationState,
    new_messages: list[BaseMessage],
) -> InvestigationState:
    """Update investigation state based on new messages.

    This is a heuristic extractor — it looks for patterns in tool outputs
    and AI responses to update the structured state. Not perfect, but
    captures the most common patterns.
    """
    for msg in new_messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)

        if isinstance(msg, HumanMessage) and not state.goal:
            # First human message = goal
            state.goal = content[:200]

        elif isinstance(msg, ToolMessage):
            # Track skill usage
            if "[SKILL ACTIVATED:" in content:
                key = _extract_skill_key(content)
                if key not in state.skills_used:
                    state.skills_used.append(key)

            # Track failures as eliminated hypotheses
            if content.startswith("❌") or getattr(msg, "status", None) == "error":
                first_line = content.split("\n")[0][:100]
                elimination = f"{msg.name}: {first_line}"
                if elimination not in state.eliminated:
                    state.eliminated.append(elimination)

            # Extract key artifacts: stack traces, file:line references
            # These are critical facts that must survive compression
            for pattern in [
                r"(\w+(?:Exception|Error))\s+at\s+([\w./]+):(\d+)",  # Java stack trace
                r"File \"([^\"]+)\", line (\d+)",  # Python traceback
                r"([\w/]+\.\w+):(\d+):\d+: error",  # GCC/Go errors
            ]:
                for match in re.finditer(pattern, content):
                    artifact = match.group(0)[:120]
                    if artifact not in state.confirmed_facts:
                        state.confirmed_facts.append(artifact)
                        # Also store as key artifact
                        groups = match.groups()
                        if len(groups) >= 2:
                            state.key_artifacts[groups[-2] if '.' in groups[-2] else groups[0]] = artifact

        elif isinstance(msg, AIMessage) and not msg.tool_calls:
            # AI conclusions may contain confirmed facts
            # Look for strong assertion patterns
            for pattern in [
                r"(?:the root cause is|confirmed:|the issue is|found:)\s*(.{10,100})",
                r"(?:this works because|solution:)\s*(.{10,100})",
            ]:
                match = re.search(pattern, content, re.IGNORECASE)
                if match:
                    fact = match.group(1).strip()
                    if fact not in state.confirmed_facts:
                        state.confirmed_facts.append(fact)

    return state
