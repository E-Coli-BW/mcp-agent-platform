"""Unit tests for app.agent.skills_index — catalog parser, cache, renderer."""

from __future__ import annotations

import json
import time

import pytest

from app.agent import skills_index
from app.agent.skills_index import (
    SkillEntry,
    _extract_summary,
    _parse_memory_search_result,
    _to_skill_entry,
    invalidate_skills_cache,
    render_skill_catalog,
)


# ── Parsers ──────────────────────────────────────────────────────────────────


class TestExtractSummary:
    def test_first_non_heading_line(self):
        body = "# Title\n\nThis is the first real line.\nSecond line."
        assert _extract_summary(body, 200) == "This is the first real line."

    def test_summary_heading_wins(self):
        body = (
            "# Skill: foo\n"
            "\n"
            "## Summary\n"
            "Use mvn clean install when JAR is stale.\n"
            "\n"
            "## Detail\n"
            "Long body text..."
        )
        assert _extract_summary(body, 200).startswith("Use mvn clean install")

    def test_problem_heading_alias(self):
        body = "## Problem\nRedis pool exhaustion under load.\n"
        assert _extract_summary(body, 200) == "Redis pool exhaustion under load."

    def test_empty_body_returns_empty(self):
        assert _extract_summary("", 100) == ""

    def test_only_headings_falls_back(self):
        body = "# Title\n## More\n"
        # Falls back to first chars of body
        assert "Title" in _extract_summary(body, 50) or _extract_summary(body, 50) == "# Title"


class TestParseMemorySearchResult:
    def test_plain_json_array(self):
        raw = '[{"key": "skill-a", "content": "hello"}]'
        out = _parse_memory_search_result(raw)
        assert len(out) == 1 and out[0]["key"] == "skill-a"

    def test_mcp_envelope(self):
        envelope = {
            "content": [{"type": "text", "text": '[{"key":"skill-b","content":"x"}]'}]
        }
        out = _parse_memory_search_result(json.dumps(envelope))
        assert len(out) == 1 and out[0]["key"] == "skill-b"

    def test_with_leading_prose(self):
        raw = 'Found 2 skills:\n[{"key":"a"},{"key":"b"}]'
        out = _parse_memory_search_result(raw)
        assert [r["key"] for r in out] == ["a", "b"]

    def test_garbage_returns_empty(self):
        assert _parse_memory_search_result("nothing here") == []

    def test_empty_input(self):
        assert _parse_memory_search_result("") == []


class TestToSkillEntry:
    def test_minimal_record(self):
        rec = {"key": "skill-x", "content": "Use foo to do bar."}
        e = _to_skill_entry(rec, 100)
        assert e is not None
        assert e.key == "skill-x"
        assert "foo" in e.summary

    def test_explicit_summary_wins(self):
        rec = {
            "key": "skill-y",
            "content": "Long body...",
            "summary": "Short summary",
        }
        e = _to_skill_entry(rec, 100)
        assert e.summary == "Short summary"

    def test_missing_key_returns_none(self):
        assert _to_skill_entry({"content": "x"}, 100) is None

    def test_invalid_tags_filtered(self):
        rec = {"key": "k", "content": "c", "tags": "not-a-list"}
        e = _to_skill_entry(rec, 100)
        assert e is not None and e.tags == []


# ── Rendering ─────────────────────────────────────────────────────────────────


class TestRenderCatalog:
    def test_empty_returns_empty_string(self):
        assert render_skill_catalog([]) == ""

    def test_renders_keys_and_summaries(self):
        entries = [
            SkillEntry(key="skill-a", summary="do thing A", tags=["debug"]),
            SkillEntry(key="skill-b", summary="do thing B", tags=[]),
        ]
        out = render_skill_catalog(entries, summary_chars=80)
        assert "SKILLS CATALOG" in out
        assert "`skill-a`" in out and "do thing A" in out
        assert "`skill-b`" in out and "do thing B" in out
        assert "skill_get" in out  # teaches the LLM the lazy-load pattern

    def test_truncates_long_summary(self):
        entries = [SkillEntry(key="k", summary="x" * 500, tags=[])]
        out = render_skill_catalog(entries, summary_chars=50)
        # The line for k must contain ellipsis and be bounded
        line = [l for l in out.splitlines() if l.startswith("- ")][0]
        assert "…" in line
        assert len(line) < 200

    def test_pinned_marker(self):
        entries = [
            SkillEntry(key="pin", summary="x", tags=[], pinned=True),
            SkillEntry(key="reg", summary="y", tags=[], pinned=False),
        ]
        out = render_skill_catalog(entries)
        # Pinned line shows the 📌 marker
        assert "📌" in out


# ── Cache ─────────────────────────────────────────────────────────────────────


class TestCache:
    def setup_method(self):
        invalidate_skills_cache()

    def test_cache_hit_within_ttl(self):
        entries = [SkillEntry(key="a", summary="s", tags=[])]
        skills_index._cache_put("tenant1:skills", entries)
        hit = skills_index._cache_get("tenant1:skills", ttl=60)
        assert hit is not None and hit[0].key == "a"

    def test_cache_miss_after_ttl(self):
        entries = [SkillEntry(key="a", summary="s", tags=[])]
        skills_index._cache_put("tenant1:skills", entries)
        # Simulate aged cache by manipulating fetched_at
        _, e = skills_index._CACHE["tenant1:skills"]
        skills_index._CACHE["tenant1:skills"] = (time.time() - 9999, e)
        assert skills_index._cache_get("tenant1:skills", ttl=60) is None

    def test_invalidate_specific_tenant(self):
        skills_index._cache_put("tenant1:skills", [])
        skills_index._cache_put("tenant2:skills", [])
        invalidate_skills_cache("tenant1:skills")
        assert "tenant1:skills" not in skills_index._CACHE
        assert "tenant2:skills" in skills_index._CACHE

    def test_invalidate_all(self):
        skills_index._cache_put("tenant1:skills", [])
        skills_index._cache_put("tenant2:skills", [])
        invalidate_skills_cache()
        assert skills_index._CACHE == {}


# ── Fetch integration (mocked memory client) ──────────────────────────────────


class _FakeClient:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    async def call_tool(self, name, args, tenant_id=None):
        self.calls += 1
        return self.payload


@pytest.fixture
def patch_memory(monkeypatch):
    def _patch(payload: str):
        client = _FakeClient(payload)
        import app.tools.definitions as defs

        monkeypatch.setattr(defs, "_get_memory", lambda: client)
        return client

    invalidate_skills_cache()
    return _patch


class TestFetchSkillCatalog:
    @pytest.mark.asyncio
    async def test_fetches_and_caches(self, patch_memory):
        payload = json.dumps(
            [
                {"key": "skill-1", "content": "First line.\nrest..."},
                {"key": "skill-2", "content": "Second line."},
            ]
        )
        client = patch_memory(payload)

        from app.agent.skills_index import fetch_skill_catalog

        # First call → hits the backend
        entries = await fetch_skill_catalog("tenantA")
        assert len(entries) == 2
        assert client.calls == 1

        # Second call → served from cache, no backend hit
        entries2 = await fetch_skill_catalog("tenantA")
        assert len(entries2) == 2
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_failure_returns_empty(self, monkeypatch):
        class _Boom:
            async def call_tool(self, *a, **kw):
                raise RuntimeError("network down")

        import app.tools.definitions as defs

        monkeypatch.setattr(defs, "_get_memory", lambda: _Boom())
        invalidate_skills_cache()

        from app.agent.skills_index import fetch_skill_catalog

        entries = await fetch_skill_catalog("tenantA")
        assert entries == []

    @pytest.mark.asyncio
    async def test_respects_max_entries(self, patch_memory):
        records = [
            {"key": f"skill-{i}", "content": f"Body {i}"} for i in range(50)
        ]
        patch_memory(json.dumps(records))

        from app.agent.skills_index import fetch_skill_catalog

        entries = await fetch_skill_catalog("tenantA", max_entries=5)
        assert len(entries) == 5
