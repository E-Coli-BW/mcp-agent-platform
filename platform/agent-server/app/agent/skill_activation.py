"""Skill Trigger Cache + Auto-Activation Logic (Layers 2 & 3).

This module implements the reactive (error-triggered) and proactive (context-triggered)
skill activation layers described in SKILL-STORE-DESIGN.md.

Layer 2: After a tool call fails, match error output against trigger_errors.
Layer 3: Before the first LLM call, match user query against trigger_patterns.

The SkillTriggerCache is loaded from the memory-server's /api/tools/skill_triggers
endpoint and refreshed every TTL seconds. Per-tenant isolation is enforced.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Data Types ────────────────────────────────────────────────────────────────


@dataclass
class SkillHint:
    """A skill suggestion to inject into the prompt."""
    key: str
    score: float
    problem_summary: str = ""


@dataclass
class TriggerEntry:
    """Cached trigger metadata for one skill."""
    key: str
    problem_summary: str
    trigger_patterns: list[dict] = field(default_factory=list)
    trigger_errors: list[dict] = field(default_factory=list)
    trigger_tools: list[str] = field(default_factory=list)


# ── Cache ─────────────────────────────────────────────────────────────────────


class SkillTriggerCache:
    """In-process, per-tenant cache of skill trigger metadata.

    Loaded from the memory-server on startup and refreshed every TTL seconds.
    Only active skills with at least one trigger field are cached.
    """

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, list[TriggerEntry]]] = {}

    def get_entries(self, tenant_id: str) -> list[TriggerEntry]:
        """Get cached entries for a tenant. Returns empty list if expired/missing."""
        rec = self._store.get(tenant_id)
        if not rec:
            return []
        fetched_at, entries = rec
        if time.time() - fetched_at > self._ttl:
            return []
        return entries

    def put(self, tenant_id: str, entries: list[TriggerEntry]) -> None:
        self._store[tenant_id] = (time.time(), entries)

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._store.clear()
        else:
            self._store.pop(tenant_id, None)

    async def ensure_loaded(self, tenant_id: str) -> list[TriggerEntry]:
        """Load from backend if cache is empty/expired."""
        entries = self.get_entries(tenant_id)
        if entries:
            return entries

        try:
            entries = await _fetch_triggers_from_backend(tenant_id)
            self.put(tenant_id, entries)
            return entries
        except Exception as e:
            logger.warning("Failed to load skill triggers: %s", e)
            return []


# ── Singleton cache instance ──────────────────────────────────────────────────

_cache = SkillTriggerCache(ttl_seconds=60.0)


def get_skill_trigger_cache() -> SkillTriggerCache:
    return _cache


# ── Backend Fetcher ───────────────────────────────────────────────────────────


async def _fetch_triggers_from_backend(tenant_id: str) -> list[TriggerEntry]:
    """Fetch trigger metadata from memory-server's skill_triggers endpoint."""
    from app.tools.definitions import _get_memory

    client = _get_memory()
    raw = await client.call_tool("skill_triggers", {}, tenant_id=tenant_id)

    if not raw:
        return []

    text = raw if isinstance(raw, str) else str(raw)
    # Parse the JSON array response
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        # Try to extract from MCP envelope
        try:
            env = json.loads(text)
            if isinstance(env, dict) and "result" in env:
                data = json.loads(env["result"])
            else:
                return []
        except (ValueError, TypeError):
            return []

    if not isinstance(data, list):
        return []

    entries = []
    for item in data:
        if not isinstance(item, dict):
            continue
        trigger_patterns = _parse_json_field(item.get("trigger_patterns"))
        trigger_errors = _parse_json_field(item.get("trigger_errors"))
        trigger_tools = _parse_json_field(item.get("trigger_tools"))

        entries.append(TriggerEntry(
            key=item.get("key", ""),
            problem_summary=item.get("problem", ""),
            trigger_patterns=trigger_patterns if isinstance(trigger_patterns, list) else [],
            trigger_errors=trigger_errors if isinstance(trigger_errors, list) else [],
            trigger_tools=trigger_tools if isinstance(trigger_tools, list) else [],
        ))

    logger.info("🔫 Loaded %d skill triggers for tenant %s", len(entries), tenant_id)
    return entries


def _parse_json_field(value) -> list | None:
    """Parse a JSON string field into a Python object."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []
    return []


# ── Layer 2: Error-Triggered Activation ───────────────────────────────────────


async def maybe_activate_skill_on_error(
    tenant_id: str,
    tool_name: str,
    error_output: str,
) -> str | None:
    """Match error output against trigger_errors of all cached skills.

    Returns a formatted skill hint string if a match is found, else None.
    Called from the tools node after a tool call returns an error.
    """
    if not error_output:
        return None

    cache = get_skill_trigger_cache()
    entries = await cache.ensure_loaded(tenant_id)

    for entry in entries:
        for pattern_obj in entry.trigger_errors:
            pattern = pattern_obj.get("pattern", "") if isinstance(pattern_obj, dict) else str(pattern_obj)
            if not pattern:
                continue
            try:
                if re.search(pattern, error_output, re.IGNORECASE):
                    # Fetch full skill body
                    body = await _fetch_skill_body(tenant_id, entry.key)
                    if body:
                        return (
                            f"[SKILL ACTIVATED: {entry.key}]\n"
                            f"You hit a known error. Follow these steps:\n\n{body}"
                        )
            except re.error:
                continue  # skip invalid regex

    return None


# ── Layer 3: Proactive Context-Triggered Activation ───────────────────────────


async def proactive_skill_match(
    tenant_id: str,
    user_message: str,
    bound_tools: set[str] | None = None,
    max_hints: int = 2,
) -> list[SkillHint]:
    """Scan user message against trigger_patterns of cached skills.

    Called from the intent_router node before routing to call_llm.
    Returns top-N matching skills (bounded to avoid prompt bloat).
    """
    if not user_message:
        return []

    cache = get_skill_trigger_cache()
    entries = await cache.ensure_loaded(tenant_id)
    bound_tools = bound_tools or set()

    hits: list[tuple[float, TriggerEntry]] = []

    for entry in entries:
        score = 0.0

        # Match trigger_patterns against user message
        for trigger in entry.trigger_patterns:
            if not isinstance(trigger, dict):
                continue
            trigger_type = trigger.get("type", "")

            if trigger_type == "keyword":
                terms = trigger.get("terms", [])
                if any(term.lower() in user_message.lower() for term in terms):
                    score += 1.0
            elif trigger_type == "regex":
                pattern = trigger.get("pattern", "")
                try:
                    if re.search(pattern, user_message, re.IGNORECASE):
                        score += 1.5
                except re.error:
                    continue

        # Tool overlap bonus
        if entry.trigger_tools and bound_tools:
            skill_tools = set(entry.trigger_tools)
            if skill_tools & bound_tools:
                score += 0.5

        if score > 0:
            hits.append((score, entry))

    # Sort by score descending, return top N
    hits.sort(key=lambda x: x[0], reverse=True)
    return [
        SkillHint(key=e.key, score=s, problem_summary=e.problem_summary)
        for s, e in hits[:max_hints]
    ]


def render_skill_hints(hints: list[SkillHint]) -> str:
    """Render skill hints as a compact prompt block."""
    if not hints:
        return ""
    lines = ["\n[SUGGESTED SKILLS — call skill_get(key) if relevant]"]
    for h in hints:
        lines.append(f"- {h.key} (score: {h.score:.1f}) — {h.problem_summary}")
    return "\n".join(lines)


# ── Helper ────────────────────────────────────────────────────────────────────


async def _fetch_skill_body(tenant_id: str, key: str) -> str | None:
    """Fetch full skill body via skill_get tool."""
    try:
        from app.tools.definitions import _get_memory

        client = _get_memory()
        raw = await client.call_tool("skill_get", {"key": key}, tenant_id=tenant_id)
        if raw:
            return raw if isinstance(raw, str) else str(raw)
    except Exception as e:
        logger.warning("Failed to fetch skill body for %s: %s", key, e)
    return None
