"""Compatibility facade from runtime events to OpenAI-compatible streaming."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request

from app.api.chat_models import ChatMessage
from app.api.openai_sse import project_runtime_event
from app.auth.middleware import AuthContext, active_auth_context, tenant_context
from app.context.request_context import (
    RequestContext,
    agent_id_ctx,
    knowledge_collection_ctx,
    prompt_version_ctx,
    request_id_ctx,
    run_id_ctx,
    session_id_ctx,
    workspace_root_ctx,
)
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
    auth_context: AuthContext | None = None,
    request_context: RequestContext | None = None,
) -> AsyncIterator[str | dict[str, Any]]:
    """Execute one runtime turn and project its events onto the OpenAI wire."""
    tenant_token = tenant_context.set(tenant_id)
    auth_token = active_auth_context.set(auth_context)
    context_vars = (
        (request_id_ctx, request_context.request_id),
        (run_id_ctx, request_context.run_id),
        (session_id_ctx, request_context.session_id),
        (prompt_version_ctx, request_context.prompt_version),
        (agent_id_ctx, request_context.agent_id),
        (knowledge_collection_ctx, request_context.knowledge_collection),
        (workspace_root_ctx, request_context.workspace_root),
    ) if request_context is not None else ()
    context_tokens = [(var, var.set(value)) for var, value in context_vars]
    try:
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
    finally:
        for var, token in reversed(context_tokens):
            var.reset(token)
        active_auth_context.reset(auth_token)
        tenant_context.reset(tenant_token)
