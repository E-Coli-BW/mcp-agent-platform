"""Model-adaptive execution profiles.

Safety invariants stay fixed in code; profiles only tune bounded autonomy and
cost/latency knobs for a model or domain pack.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    max_tool_calls: int
    max_context_chars: int
    verification_enabled: bool
    allow_subagents: bool
    direct_tool_routing_enabled: bool


INVARIANT_POLICY = ("approval_gate", "tenant_isolation", "idempotency", "no_cot")


def _as_bool(value, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def resolve_runtime_profile(config, settings) -> RuntimeProfile:
    raw = getattr(config, "runtime", {}) or {}
    max_calls = int(raw.get("max_tool_calls", config.guardrails.get("max_tool_calls", settings.max_agent_steps)))
    return RuntimeProfile(
        name=raw.get("name", config.id),
        max_tool_calls=max(1, min(max_calls, settings.max_agent_steps)),
        max_context_chars=int(raw.get("max_context_chars", config.guardrails.get("max_context_chars", settings.max_context_chars))),
        verification_enabled=_as_bool(
            raw.get("verification_enabled"),
            field="verification_enabled",
        ),
        allow_subagents=_as_bool(
            raw.get("allow_subagents"),
            field="allow_subagents",
        ),
        direct_tool_routing_enabled=_as_bool(
            raw.get("direct_tool_routing_enabled"),
            field="direct_tool_routing_enabled",
            default=getattr(settings, "direct_tool_routing_enabled", False),
        ),
    )
