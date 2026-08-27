"""OpenAI-compatible chat HTTP adapter."""

import json
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.agent.intent import get_meta_answer, is_meta_question
from app.api import openai_sse
from app.api.chat_models import ActiveFileContext, ChatMessage, ChatRequest
from app.auth.middleware import AuthContext, require_auth, tenant_context
from app.context.request_context import set_prompt_version, set_request_context
from app.runtime.chat_stream import stream_agent_response as _stream_agent_response
from app.store.conversation import Message, get_conversation_store

_FinalResponseBuffer = openai_sse.FinalResponseBuffer
_ToolEventTracker = openai_sse.ToolEventTracker
_make_chunk = openai_sse.make_chunk
_verified_artifact_answer_from_chain_event = (
    openai_sse.verified_artifact_answer_from_chain_event
)

__all__ = [
    "ActiveFileContext",
    "ChatMessage",
    "ChatRequest",
    "_FinalResponseBuffer",
    "_ToolEventTracker",
    "_make_chunk",
    "_stream_agent_response",
    "_verified_artifact_answer_from_chain_event",
    "chat_completions",
    "router",
]

router = APIRouter()
AUTH_DEPENDENCY = Depends(require_auth)


@router.post("/v1/chat/completions")
async def chat_completions(
    raw_request: Request,
    request: ChatRequest,
    auth: AuthContext = AUTH_DEPENDENCY,
):
    """Adapt an OpenAI chat request to a tenant-scoped agent turn."""
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages is required")

    session_id = request.session_id or f"session-{uuid.uuid4().hex[:12]}"
    session_id = f"{auth.tenant_id}:{session_id}"
    tenant_context.set(auth.tenant_id)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    from app.tools.agent_mode import get_workspace_root

    workspace_root = get_workspace_root()
    set_request_context(
        request_id=request_id,
        run_id=run_id,
        session_id=session_id,
        workspace_root=workspace_root,
    )
    set_prompt_version(request.prompt_version or "")

    from app.agent.delegation.governance import init_root_context
    from app.registry.tool_registry import get_registered_tools

    init_root_context(
        root_session_id=session_id,
        allowed_tools=[
            name
            for name in get_registered_tools()
            if name != "spawn_subagent"
        ],
    )

    user_input = request.messages[-1].content
    if request.active_file:
        active_file = request.active_file
        file_hint = f"[Active file: {active_file.path}"
        if active_file.visible_start is not None:
            file_hint += f", lines {active_file.visible_start}-{active_file.visible_end}"
        file_hint += "]\n"
        user_input = file_hint + user_input
        request.messages[-1] = ChatMessage(
            role=request.messages[-1].role,
            content=user_input,
        )

    store = get_conversation_store()
    if is_meta_question(user_input):
        answer = get_meta_answer(user_input)
        if answer:
            await store.append(session_id, Message(role="user", content=user_input))
            await store.append(session_id, Message(role="assistant", content=answer))

            if request.stream:

                async def meta_stream():
                    yield _make_chunk(answer, request.model)
                    yield _make_chunk("", request.model, finish_reason="stop")
                    yield "[DONE]"

                return EventSourceResponse(
                    meta_stream(),
                    media_type="text/event-stream",
                    headers={"X-Run-ID": run_id, "X-Request-ID": request_id},
                )
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "run_id": run_id,
                "request_id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }

    stream = _stream_agent_response(
        request.messages,
        request.model,
        session_id,
        auth.tenant_id,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        prompt_version=request.prompt_version,
        http_request=raw_request,
    )
    if request.stream:
        return EventSourceResponse(
            stream,
            media_type="text/event-stream",
            headers={"X-Run-ID": run_id, "X-Request-ID": request_id},
        )

    full_response = ""
    async for chunk in stream:
        if chunk == "[DONE]":
            break
        if isinstance(chunk, dict):
            continue
        try:
            payload = json.loads(chunk)
            content = payload.get("choices", [{}])[0].get("delta", {}).get("content", "")
            content = re.sub(r"<!-- TOOL:.*? -->\s*", "", content, flags=re.DOTALL)
            content = re.sub(r"\U0001f527 Using \w+\.\.\.\s*", "", content)
            full_response += content
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "run_id": run_id,
        "request_id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": full_response},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@router.get("/v1/models")
async def list_models():
    """List the locally hosted coding agent model."""
    return {
        "object": "list",
        "data": [{"id": "coding-agent", "object": "model", "owned_by": "local"}],
    }
