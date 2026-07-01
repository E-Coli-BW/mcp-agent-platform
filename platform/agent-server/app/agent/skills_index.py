"""Skills index — compact catalog of available skills for system-prompt injection.

WHY: The legacy approach was to either (a) inject all skill *bodies* into the
system prompt — which blows up context once you have more than a handful of
skills — or (b) rely on the LLM to call ``memory_search`` blindly. Both fail
at scale.

DESIGN: Catalog + lazy load.

    System prompt → [skill_catalog metadata only: key, summary, tags]
                                     ↓
                  LLM picks a skill it needs
                                     ↓
                  LLM calls skill_get(key) → full skill body

This keeps the prompt bounded (~40 entries × ~100 chars ≈ 4KB ≈ 1K tokens) while
giving the LLM full discoverability. It mirrors how human engineers use a
documentation index: scan the TOC first, open only the page you need.

Cache:
- The catalog is fetched from the memory server (namespace=skills) and cached
  in-process for ``settings.skills_index_ttl_seconds``. Multi-tenant safe: the
  cache is keyed by ``tenant_id`` so tenant A's skills are never visible to B.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class SkillEntry:
    key: str
    summary: str
    tags: list[str]
    pinned: bool = False
    updated_at: str = ""

    def render_index_line(self, summary_chars: int) -> str:
        """One line in the prompt catalog. Format: `- key — summary [tags]`."""
        summary = (self.summary or "").strip().replace("\n", " ")
        if len(summary) > summary_chars:
            summary = summary[: summary_chars - 1] + "…"
        tag_str = ""
        if self.tags:
            tag_str = " [" + ", ".join(self.tags[:4]) + "]"
        pin = "📌 " if self.pinned else ""
        return f"- {pin}`{self.key}` — {summary}{tag_str}"


# ── Cache (per-tenant) ────────────────────────────────────────────────────────


_CACHE: dict[str, tuple[float, list[SkillEntry]]] = {}


def _cache_get(tenant_id: str, ttl: float) -> list[SkillEntry] | None:
    rec = _CACHE.get(tenant_id)
    if not rec:
        return None
    fetched_at, entries = rec
    if time.time() - fetched_at > ttl:
        return None
    return entries


def _cache_put(tenant_id: str, entries: list[SkillEntry]) -> None:
    _CACHE[tenant_id] = (time.time(), entries)


def invalidate_skills_cache(tenant_id: str | None = None) -> None:
    """Force a refetch on next request. Call this after skill_set / skill_delete."""
    if tenant_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(tenant_id, None)


# ── Parsers ───────────────────────────────────────────────────────────────────


_SUMMARY_HEADING_RE = re.compile(
    r"^\s*##?\s*(?:summary|problem|tl;?dr)\b[:\s]*\n+(.+?)(?:\n##|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _extract_summary(content: str, fallback_chars: int) -> str:
    """Pull a 1-line summary from a skill body.

    Preference order:
      1. The first non-empty line under a `## Summary` / `## Problem` / `## TL;DR`
         heading.
      2. The first non-empty, non-heading line.
      3. The first ``fallback_chars`` of the body.
    """
    if not content:
        return ""

    m = _SUMMARY_HEADING_RE.search(content)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-*").strip()
            if line:
                return line

    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.lstrip("-*").strip()

    return content[:fallback_chars].strip()


def _parse_memory_search_result(raw: str) -> list[dict]:
    """The memory server returns a textual envelope; pull out the JSON list.

    Tolerates:
      - leading prose ("📋 Found 5 skills:")
      - markdown code fences
      - text-only output (returns empty list)
    """
    if not raw:
        return []

    text = raw.strip()

    # Strip MCP content envelopes if present, e.g. {"content":[{"type":"text","text":"..."}]}
    try:
        env = json.loads(text)
        if isinstance(env, dict) and "content" in env:
            parts = env["content"]
            if isinstance(parts, list) and parts:
                text = parts[0].get("text", "") if isinstance(parts[0], dict) else str(parts[0])
    except (ValueError, TypeError):
        pass

    # Strip code fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)

    # Find the first JSON array in the payload
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []

    if not isinstance(parsed, list):
        return []

    out: list[dict] = []
    for item in parsed:
        if isinstance(item, dict):
            out.append(item)
    return out


def _to_skill_entry(record: dict, summary_chars: int) -> SkillEntry | None:
    """Convert a memory store record into a SkillEntry."""
    key = record.get("key") or record.get("id")
    if not key:
        return None
    content = record.get("content", "") or ""
    summary = record.get("summary") or _extract_summary(content, summary_chars)
    tags = record.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return SkillEntry(
        key=str(key),
        summary=str(summary),
        tags=[str(t) for t in tags],
        pinned=bool(record.get("pinned", False)),
        updated_at=str(record.get("updated_at", "")),
    )


# ── Public API ────────────────────────────────────────────────────────────────


async def fetch_skill_catalog(
    tenant_id: str,
    *,
    namespace: str | None = None,
    max_entries: int | None = None,
    summary_chars: int | None = None,
    use_cache: bool = True,
) -> list[SkillEntry]:
    """Fetch the skill catalog for ``tenant_id``.

    Returns at most ``max_entries`` skills, with pinned + recently-updated
    entries first. Never raises — on any failure returns an empty list and
    logs a warning. (Skills are nice-to-have context, not request-critical.)
    """
    from app.config import settings

    namespace = namespace or settings.skills_namespace
    max_entries = max_entries or settings.skills_index_max_entries
    summary_chars = summary_chars or settings.skills_index_summary_chars
    ttl = settings.skills_index_ttl_seconds

    cache_key = f"{tenant_id}:{namespace}"
    if use_cache:
        hit = _cache_get(cache_key, ttl)
        if hit is not None:
            return hit[:max_entries]

    try:
        from app.tools.definitions import _get_memory

        client = _get_memory()
        # We use a broad query; the server returns recently-touched entries first.
        raw = await client.call_tool(
            "memory_search",
            {"query": "*", "namespace": namespace, "limit": max_entries * 2},
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.warning("Skill catalog fetch failed: %s", e)
        _cache_put(cache_key, [])
        return []

    records = _parse_memory_search_result(raw if isinstance(raw, str) else str(raw))
    entries: list[SkillEntry] = []
    for rec in records:
        entry = _to_skill_entry(rec, summary_chars)
        if entry is not None:
            entries.append(entry)

    # Sort: pinned first, then by updated_at desc
    entries.sort(key=lambda e: (not e.pinned, e.updated_at), reverse=False)
    # The above puts pinned (False=0 first) ahead, but updated_at asc is wrong.
    # Re-sort with the right key:
    entries.sort(key=lambda e: e.updated_at, reverse=True)
    entries.sort(key=lambda e: e.pinned, reverse=True)

    entries = entries[:max_entries]
    _cache_put(cache_key, entries)
    return entries


def render_skill_catalog(
    entries: list[SkillEntry], summary_chars: int | None = None
) -> str:
    """Render entries as a system-prompt block. Empty if no entries."""
    if not entries:
        return ""

    from app.config import settings

    summary_chars = summary_chars or settings.skills_index_summary_chars

    lines = [
        "## SKILLS CATALOG",
        (
            "You have access to a library of saved skills (reusable workflows from "
            "past sessions). Listed below is the INDEX ONLY — each entry shows a key "
            "and a one-line summary. To read the FULL body of any skill, call "
            "`skill_get(key=\"...\")`. Do NOT assume you already know the contents."
        ),
        "",
    ]
    for entry in entries:
        lines.append(entry.render_index_line(summary_chars))
    return "\n".join(lines)


async def build_skill_catalog_block(tenant_id: str) -> str:
    """Convenience: fetch + render in one call. Returns '' when disabled or empty."""
    from app.config import settings

    if not settings.skills_index_enabled:
        return ""

    entries = await fetch_skill_catalog(tenant_id)
    return render_skill_catalog(entries)


__all__ = [
    "SkillEntry",
    "fetch_skill_catalog",
    "render_skill_catalog",
    "build_skill_catalog_block",
    "invalidate_skills_cache",
]
