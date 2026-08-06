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


INVARIANT_POLICY = ("approval_gate", "tenant_isolation", "idempotency", "no_cot")


def resolve_runtime_profile(config, settings) -> RuntimeProfile:
    raw = getattr(config, "runtime", {}) or {}
    max_calls = int(raw.get("max_tool_calls", config.guardrails.get("max_tool_calls", settings.max_agent_steps)))
    return RuntimeProfile(
        name=raw.get("name", config.id),
        max_tool_calls=max(1, min(max_calls, settings.max_agent_steps)),
        max_context_chars=int(raw.get("max_context_chars", config.guardrails.get("max_context_chars", settings.max_context_chars))),
        verification_enabled=bool(raw.get("verification_enabled", False)),
        allow_subagents=bool(raw.get("allow_subagents", False)),
    )
