"""Per-request context vars for governance and provenance.

These context vars are intentionally lightweight and optional:
- APIs set them at request entry.
- deep runtime code reads them without tight coupling to FastAPI request objects.
- defaults are empty/None so non-HTTP code paths still work.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
run_id_ctx: ContextVar[str] = ContextVar("run_id", default="")
session_id_ctx: ContextVar[str] = ContextVar("session_id", default="")
# Explicit prompt version chosen for this request after governance resolution.
prompt_version_ctx: ContextVar[str] = ContextVar("prompt_version", default="")


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    run_id: str
    session_id: str
    prompt_version: str


def set_request_context(request_id: str, run_id: str, session_id: str) -> None:
    request_id_ctx.set(request_id)
    run_id_ctx.set(run_id)
    session_id_ctx.set(session_id)


def set_prompt_version(version: str) -> None:
    prompt_version_ctx.set(version or "")


def get_request_context() -> RequestContext:
    return RequestContext(
        request_id=request_id_ctx.get(),
        run_id=run_id_ctx.get(),
        session_id=session_id_ctx.get(),
        prompt_version=prompt_version_ctx.get(),
    )

