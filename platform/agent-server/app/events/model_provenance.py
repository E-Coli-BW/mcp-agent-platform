"""Model-call provenance helpers.

Provides a stable event envelope so every model invocation can be traced and
reproduced by request/session/run identifiers plus prompt/model metadata.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)

_MODEL_CALL_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "design"
    / "schemas"
    / "model-call-provenance.schema.json"
)


@lru_cache(maxsize=1)
def _load_model_call_schema() -> dict[str, Any] | None:
    try:
        return json.loads(_MODEL_CALL_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Model-call schema not found at %s", _MODEL_CALL_SCHEMA_PATH)
        return None
    except Exception as exc:
        logger.warning("Failed to load model-call schema: %s", exc)
        return None


@lru_cache(maxsize=1)
def _get_model_call_validator():
    schema = _load_model_call_schema()
    if schema is None:
        return None
    try:
        from jsonschema import Draft202012Validator

        return Draft202012Validator(schema)
    except Exception as exc:
        logger.warning("jsonschema validator unavailable: %s", exc)
        return None


def validate_model_call_event(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate a model-call event against model-call-provenance.schema.json."""
    validator = _get_model_call_validator()
    if validator is None:
        # Validation is best-effort; schema load issues must not break chat responses.
        return True, None

    errors = sorted(validator.iter_errors(payload), key=lambda err: list(err.path))
    if not errors:
        return True, None

    first = errors[0]
    location = ".".join(str(part) for part in first.path) or "<root>"
    return False, f"{location}: {first.message}"


def infer_provider(model_name: str) -> str:
    if model_name.startswith("openai/") or model_name.startswith("gpt-"):
        return "openai"
    if model_name.startswith("anthropic/") or model_name.startswith("claude"):
        return "anthropic"
    return "ollama"


def current_trace_id() -> str | None:
    """Best-effort trace id extraction from current OpenTelemetry span."""
    try:
        from opentelemetry.trace import get_current_span

        span = get_current_span()
        if span is None:
            return None
        ctx = span.get_span_context()
        if not ctx or not getattr(ctx, "is_valid", False):
            return None
        return format(ctx.trace_id, "032x")
    except Exception:
        return None


def build_feature_flags_snapshot() -> dict[str, Any]:
    """Capture effective runtime behavior flags for reproducibility."""
    return {
        "agent_graph_version": settings.agent_graph_version,
        "prompt_version_default": settings.prompt_version,
        "direct_tool_routing_enabled": settings.direct_tool_routing_enabled,
        "reflexion_enabled": settings.reflexion_enabled,
        "subagent_verifier_enabled": settings.subagent_verifier_enabled,
        "rerank_strategy": settings.rerank_strategy,
        "max_context_chars": settings.max_context_chars,
        "max_agent_steps": settings.max_agent_steps,
    }


def make_model_call_event(
    *,
    run_id: str,
    request_id: str,
    trace_id: str | None,
    tenant_id: str,
    session_id: str,
    runtime: str,
    call_site: str,
    provider: str,
    model: str,
    temperature: float,
    max_tokens: int | None,
    prompt_id: str,
    prompt_version: str,
    prompt_hash: str,
    feature_flags: dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    status: str,
    error_class: str | None = None,
) -> dict[str, Any]:
    normalized_max_tokens = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else None
    return {
        "event_id": f"mcall-{uuid.uuid4().hex}",
        "run_id": run_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "runtime": runtime,
        "call_site": call_site,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "max_tokens": normalized_max_tokens,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "feature_flags": feature_flags,
        "prompt_tokens": max(0, int(prompt_tokens)),
        "completion_tokens": max(0, int(completion_tokens)),
        "duration_ms": max(0, int(duration_ms)),
        "status": status,
        "error_class": error_class,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }


