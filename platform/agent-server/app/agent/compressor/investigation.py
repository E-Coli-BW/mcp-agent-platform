"""Durable investigation state and heuristic fact extraction."""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field


class InvestigationState(BaseModel):
    """Structured facts that survive model-view message compression."""

    goal: str = ""
    confirmed_facts: list[str] = Field(default_factory=list)
    current_hypothesis: str = ""
    eliminated: list[str] = Field(default_factory=list)
    key_artifacts: dict[str, str] = Field(default_factory=dict)
    skills_used: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)

    def to_summary_block(self) -> str:
        lines = []
        if self.goal:
            lines.append(f"**Goal**: {self.goal}")
        if self.confirmed_facts:
            lines.append("**Confirmed facts**:")
            lines.extend(f"  - {fact}" for fact in self.confirmed_facts[-10:])
        if self.current_hypothesis:
            lines.append(f"**Current hypothesis**: {self.current_hypothesis}")
        if self.eliminated:
            lines.append("**Eliminated**:")
            lines.extend(f"  - {item}" for item in self.eliminated[-5:])
        if self.key_artifacts:
            lines.append("**Key artifacts**:")
            lines.extend(
                f"  - {name}: {snippet[:100]}"
                for name, snippet in list(self.key_artifacts.items())[-5:]
            )
        if self.skills_used:
            lines.append(f"**Skills used**: {', '.join(self.skills_used)}")
        if self.next_steps:
            lines.append("**Next steps**:")
            lines.extend(f"  - {step}" for step in self.next_steps[:3])
        return "\n".join(lines) if lines else ""

    def token_estimate(self) -> int:
        return len(self.to_summary_block()) // 4


def _extract_skill_key(content: str) -> str:
    match = re.search(r"\[SKILL ACTIVATED:\s*([^\]]+)\]", content)
    return match.group(1).strip() if match else "unknown"


def update_investigation_from_messages(
    state: InvestigationState,
    new_messages: list[BaseMessage],
) -> InvestigationState:
    """Update durable facts from new human, tool, and model messages."""
    for message in new_messages:
        content = (
            message.content
            if isinstance(message.content, str)
            else str(message.content)
        )
        if isinstance(message, HumanMessage) and not state.goal:
            state.goal = content[:200]
        elif isinstance(message, ToolMessage):
            _update_from_tool_message(state, message, content)
        elif isinstance(message, AIMessage) and not message.tool_calls:
            _update_from_model_message(state, content)
    return state


def _update_from_tool_message(
    state: InvestigationState,
    message: ToolMessage,
    content: str,
) -> None:
    if "[SKILL ACTIVATED:" in content:
        key = _extract_skill_key(content)
        if key not in state.skills_used:
            state.skills_used.append(key)

    if content.startswith("❌") or getattr(message, "status", None) == "error":
        elimination = f"{message.name}: {content.splitlines()[0][:100]}"
        if elimination not in state.eliminated:
            state.eliminated.append(elimination)

    patterns = [
        r"(\w+(?:Exception|Error))\s+at\s+([\w./]+):(\d+)",
        r'File "([^"]+)", line (\d+)',
        r"([\w/]+\.\w+):(\d+):\d+: error",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            artifact = match.group(0)[:120]
            if artifact in state.confirmed_facts:
                continue
            state.confirmed_facts.append(artifact)
            groups = match.groups()
            if len(groups) >= 2:
                key = groups[-2] if "." in groups[-2] else groups[0]
                state.key_artifacts[key] = artifact


def _update_from_model_message(state: InvestigationState, content: str) -> None:
    patterns = [
        r"(?:the root cause is|confirmed:|the issue is|found:)\s*(.{10,100})",
        r"(?:this works because|solution:)\s*(.{10,100})",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            fact = match.group(1).strip()
            if fact not in state.confirmed_facts:
                state.confirmed_facts.append(fact)


InvestigationState.__module__ = "app.agent.compressor"
