"""Context injection, investigation updates, and model-view budgeting."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

from app.agent.agent_graph.state import (
    AgentState,
    ContextBudgetExceededError,
    LlmMessageView,
)
from app.agent.compressor import compress_messages, update_investigation_from_messages
from app.config import settings

logger = logging.getLogger(__name__)


async def inject_context_node(state: AgentState) -> dict[str, Any]:
    """Best-effort asynchronous workspace and skills-catalog injection."""
    needs_workspace = not state.workspace_context
    needs_skills = not state.skills_catalog
    if not needs_workspace and not needs_skills:
        return {}

    async def load_workspace() -> str:
        try:
            from app.context.workspace import get_workspace_context
            from app.tools.agent_mode import get_workspace_root

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                get_workspace_context,
                get_workspace_root(),
            )
        except Exception as error:  # noqa: BLE001 - optional context boundary
            logger.debug("Workspace context unavailable: %s", error)
            return ""

    async def load_skills() -> str:
        try:
            from app.agent.skills.catalog import build_skill_catalog_block
            from app.auth.middleware import tenant_context

            return await build_skill_catalog_block(tenant_context.get() or "default")
        except Exception as error:  # noqa: BLE001 - optional context boundary
            logger.debug("Skills catalog unavailable: %s", error)
            return ""

    tasks = []
    if needs_workspace:
        tasks.append(load_workspace())
    if needs_skills:
        tasks.append(load_skills())
    results = await asyncio.gather(*tasks, return_exceptions=False)

    output: dict[str, Any] = {}
    result_index = 0
    if needs_workspace:
        workspace_context = results[result_index]
        result_index += 1
        logger.info("Workspace context injected (%d chars)", len(workspace_context))
        output["workspace_context"] = workspace_context
    if needs_skills:
        skills_catalog = results[result_index]
        if skills_catalog:
            logger.info("Skills catalog injected (%d chars)", len(skills_catalog))
        output["skills_catalog"] = skills_catalog
    return output


def reset_turn_state_node(state: AgentState) -> dict[str, Any]:
    """Reset request-scoped counters without touching checkpointed context."""
    return {
        "loop_counter": 0,
        "consecutive_errors": 0,
        "critique_attempts": 0,
        "compression_summary": "",
    }


def update_investigation_node(state: AgentState) -> dict[str, Any]:
    """Extract durable investigation facts without mutating messages."""
    investigation = state.investigation.model_copy(deep=True)
    before = investigation.model_dump()
    update_investigation_from_messages(investigation, list(state.messages))
    if investigation.model_dump() == before:
        return {}
    return {"investigation": investigation}


def _truncate_optional_context(context: str, max_chars: int) -> str:
    if len(context) <= max_chars:
        return context
    if max_chars <= 0:
        return ""
    marker = "\n...[optional context truncated]"
    if max_chars <= len(marker):
        return context[:max_chars]
    return context[: max_chars - len(marker)] + marker


def _build_llm_view(state: AgentState, system_prompt: str) -> LlmMessageView:
    """Build an ephemeral, fully budgeted message view for the model."""
    messages = list(state.messages)
    max_chars = max(0, int(settings.max_context_chars))

    optional_context = ""
    if state.skills_catalog:
        optional_context += "\n\n" + state.skills_catalog
    if state.workspace_context:
        optional_context += "\n\nWORKSPACE:\n" + state.workspace_context

    full_prompt = system_prompt + optional_context
    compression = compress_messages(
        messages,
        budget_chars=max(0, max_chars - len(full_prompt)),
        investigation=state.investigation,
    )
    summary_lines = [compression.summary] if compression.summary else []
    total_chars = len(full_prompt) + compression.used_chars
    if total_chars > max_chars and optional_context:
        optional_budget = max(
            0,
            max_chars - len(system_prompt) - compression.used_chars,
        )
        truncated_context = _truncate_optional_context(
            optional_context,
            optional_budget,
        )
        if truncated_context != optional_context:
            summary_lines.append(
                "[optional context truncated: "
                f"{len(optional_context) - len(truncated_context)} chars]"
            )
        full_prompt = system_prompt + truncated_context
        total_chars = len(full_prompt) + compression.used_chars

    return LlmMessageView(
        messages=[SystemMessage(content=full_prompt)] + compression.messages,
        compression_summary="\n".join(summary_lines),
        overflow=total_chars > max_chars,
        total_chars=total_chars,
        budget_chars=max_chars,
    )


def _build_llm_messages(
    state: AgentState,
    system_prompt: str,
) -> list[BaseMessage]:
    """Return a validated model view for compatibility callers."""
    view = _build_llm_view(state, system_prompt)
    if view.overflow:
        raise ContextBudgetExceededError(
            "Required context floor exceeds budget: "
            f"{view.total_chars} > {view.budget_chars} chars"
        )
    return view.messages