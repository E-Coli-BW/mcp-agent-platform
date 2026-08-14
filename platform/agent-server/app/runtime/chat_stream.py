"""Compatibility facade from runtime events to OpenAI-compatible streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from app.api.chat_models import ChatMessage
from app.api.openai_sse import project_runtime_event
from app.runtime.composition import get_run_coordinator
from app.runtime.runner import RuntimeTurnRequest


async def stream_agent_response(
    messages: list[ChatMessage],
    model: str,
    session_id: str,
    tenant_id: str = "default",
    temperature: float | None = None,
    max_tokens: int | None = None,
    prompt_version: str | None = None,
    http_request: Request | None = None,
) -> AsyncIterator[str | dict[str, Any]]:
    """Execute one runtime turn and project its events onto the OpenAI wire."""
    coordinator = get_run_coordinator()
    disconnect_check = (
        http_request.is_disconnected if http_request is not None else None
    )
    request = RuntimeTurnRequest(
        model=model,
        session_id=session_id,
        tenant_id=tenant_id,
        user_input=messages[-1].content if messages else "",
        temperature=temperature,
        max_tokens=max_tokens,
        prompt_version=prompt_version,
        is_disconnected=disconnect_check,
    )
    async for event in coordinator.stream_turn(request):
        yield project_runtime_event(event)
