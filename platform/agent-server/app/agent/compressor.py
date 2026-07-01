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
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

logger = logging.getLogger(__name__)


# ── Investigation State (structured extraction) ───────────────────────────────


@dataclass
class InvestigationState:
    """Structured extraction of the current investigation.

    Updated after every meaningful tool call. Survives compression because
    it's stored as a dedicated state field, not in the message list.

    This gives the compressor a "ground truth" of what must be retained,
    making lossy compression safe.
    """

    goal: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    current_hypothesis: str = ""
    eliminated: list[str] = field(default_factory=list)  # "X because Y"
    key_artifacts: dict[str, str] = field(default_factory=dict)  # filename → snippet
    skills_used: list[str] = field(default_factory=list)  # skill keys applied
    next_steps: list[str] = field(default_factory=list)

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


# ── Compression Logic ─────────────────────────────────────────────────────────


def estimate_tokens(messages: list[BaseMessage] | str) -> int:
    """Rough token estimation: 4 chars ≈ 1 token."""
    if isinstance(messages, str):
        return len(messages) // 4
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        total += len(content) // 4
    return total


def compress_messages(
    messages: list[BaseMessage],
    budget_tokens: int,
    investigation: InvestigationState | None = None,
) -> tuple[list[BaseMessage], str]:
    """Compress message history to fit within budget.

    Returns:
        (compressed_messages, summary_of_dropped)

    Strategy:
        1. Always keep: first HumanMessage (original goal), last 6 messages
        2. Skill activations → one-line references
        3. Large tool outputs (>500 chars) older than last 6 → truncate to 200 chars
        4. Failed tool calls → "❌ [tool] failed: [first line of error]"
        5. Middle conversation turns → summarize to key points
    """
    if not messages:
        return messages, ""

    current_tokens = estimate_tokens(messages)
    if current_tokens <= budget_tokens:
        return messages, ""

    # Split into: first message (goal), middle, recent tail
    first_human_idx = next(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), 0
    )
    # Keep last 6 messages verbatim (active working context)
    tail_size = min(6, len(messages))
    tail = messages[-tail_size:]
    middle = messages[first_human_idx + 1 : -tail_size] if len(messages) > tail_size + 1 else []
    head = messages[: first_human_idx + 1]

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

    # Build final compressed list
    result = head + compressed_middle + tail

    # If we have investigation state, inject confirmed facts as a synthetic message
    # so the LLM retains critical info even after aggressive compression
    if investigation and investigation.confirmed_facts:
        facts_block = "[INVESTIGATION STATE — retained from earlier turns]\n" + investigation.to_summary_block()
        facts_msg = HumanMessage(content=facts_block)
        # Insert after head, before compressed middle
        result = head + [facts_msg] + compressed_middle + tail

    summary = "\n".join(summary_lines) if summary_lines else ""

    # If still over budget, drop oldest middle messages
    while estimate_tokens(result) > budget_tokens and len(compressed_middle) > 0:
        dropped = compressed_middle.pop(0)
        content = dropped.content if isinstance(dropped.content, str) else str(dropped.content)
        summary_lines.append(f"[dropped {type(dropped).__name__}: {content[:50]}...]")
        result = head + compressed_middle + tail
        summary = "\n".join(summary_lines)

    new_tokens = estimate_tokens(result)
    logger.info(
        "📦 Compression: %d → %d tokens (%.0f%% reduction), %d items summarized",
        current_tokens, new_tokens,
        (1 - new_tokens / current_tokens) * 100 if current_tokens > 0 else 0,
        len(summary_lines),
    )

    return result, summary


def _extract_skill_key(content: str) -> str:
    """Extract skill key from '[SKILL ACTIVATED: key-name]' block."""
    match = re.search(r"\[SKILL ACTIVATED:\s*([^\]]+)\]", content)
    return match.group(1).strip() if match else "unknown"


# ── Graph Node Factory ────────────────────────────────────────────────────────


def make_context_compressor_node(budget_tokens: int = 4000, threshold: float = 0.75):
    """Creates a graph node that compresses context when budget is exceeded.

    Args:
        budget_tokens: Maximum token budget for conversation history.
        threshold: Trigger compression when usage exceeds this fraction of budget.
    """

    def context_compressor(state) -> dict[str, Any]:
        """Conditionally compress conversation history."""
        messages = list(state.messages)
        current_tokens = estimate_tokens(messages)
        trigger_at = int(budget_tokens * threshold)

        if current_tokens <= trigger_at:
            return {}  # no-op

        logger.info(
            "🗜️ Context compressor triggered: %d tokens > %d threshold",
            current_tokens, trigger_at,
        )

        # Get investigation state if available
        investigation = getattr(state, "investigation", None)

        compressed, summary = compress_messages(messages, budget_tokens, investigation)

        result: dict[str, Any] = {"messages": compressed}

        # Store summary for reference
        if summary:
            result["compression_summary"] = summary

        return result

    return context_compressor


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
