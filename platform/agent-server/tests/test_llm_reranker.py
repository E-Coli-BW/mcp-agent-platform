"""Unit tests for app.rag.reranking.llm.

These tests do NOT call a real LLM. They mock `_create_chat_model` and exercise
the prompt construction, output parsing, and graceful-degradation paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.rag.reranking.llm import _build_user_prompt, _parse_indices, llm_rerank
from app.rag.index.retriever import ScoredChunk


def _chunk(name: str, content: str = "body", score: float = 0.5) -> ScoredChunk:
    return ScoredChunk(
        content=content,
        file_path=f"src/{name}.py",
        language="python",
        name=name,
        chunk_type="function",
        start_line=1,
        end_line=10,
        score=score,
    )


# ── Parser ────────────────────────────────────────────────────────────────────


class TestParseIndices:
    def test_plain_json_array(self):
        assert _parse_indices("[2, 0, 1]", 3) == [2, 0, 1]

    def test_with_markdown_fence(self):
        raw = "```json\n[1, 0, 2]\n```"
        assert _parse_indices(raw, 3) == [1, 0, 2]

    def test_with_leading_prose(self):
        raw = "Here are the rankings: [2, 1, 0]"
        assert _parse_indices(raw, 3) == [2, 1, 0]

    def test_dedupes_and_filters_out_of_range(self):
        raw = "[2, 2, 5, 0]"  # 5 is out of range, 2 is duplicated
        out = _parse_indices(raw, 3)
        assert out[0] == 2
        assert 0 in out
        assert 1 in out
        assert 5 not in out
        assert len(out) == 3

    def test_fills_missing_positions_from_original(self):
        raw = "[1]"  # only one index provided
        out = _parse_indices(raw, 4)
        assert out[0] == 1
        # Remaining 0, 2, 3 in original order
        assert out[1:] == [0, 2, 3]

    def test_garbage_input_returns_identity(self):
        raw = "I don't know how to rank these"
        out = _parse_indices(raw, 5)
        assert out == [0, 1, 2, 3, 4]


# ── Prompt construction ──────────────────────────────────────────────────────


class TestPromptBuilding:
    def test_truncates_long_passages(self):
        long_content = "x" * 5000
        chunks = [_chunk("big", long_content)]
        prompt = _build_user_prompt("query", chunks, max_chars=100)
        # Should contain truncation marker and not the full content
        assert "..." in prompt
        assert "x" * 5000 not in prompt

    def test_includes_all_indices(self):
        chunks = [_chunk(f"f{i}") for i in range(5)]
        prompt = _build_user_prompt("query", chunks, max_chars=200)
        for i in range(5):
            assert f"[{i}]" in prompt


# ── End-to-end with mocked model ──────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeModel:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _FakeResponse(self._response_text)


class _HangingModel:
    async def ainvoke(self, messages):
        # Hang forever — the timeout should kick in
        await asyncio.sleep(60)
        return _FakeResponse("[0]")


class _ErrorModel:
    async def ainvoke(self, messages):
        raise RuntimeError("boom")


@pytest.fixture
def patch_model(monkeypatch):
    """Returns a helper that swaps the model factory for a given fake."""

    def _patch(fake):
        import app.agent.graph as graph_mod

        # Mock signature must accept the new `temperature` kwarg that
        # callers now pass through (temperature-plumbing fix). Defaults None for
        # back-compat with any call site that doesn't supply it.
        monkeypatch.setattr(
            graph_mod,
            "_create_chat_model",
            lambda name=None, temperature=None: fake,
        )

    return _patch


class TestLlmRerank:
    @pytest.mark.asyncio
    async def test_happy_path_reorders(self, patch_model):
        patch_model(_FakeModel("[2, 0, 1]"))
        chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
        result = await llm_rerank("query", chunks, top_k=3)
        assert [c.name for c in result] == ["c", "a", "b"]
        # Scores should be re-assigned descending
        assert result[0].score > result[1].score > result[2].score

    @pytest.mark.asyncio
    async def test_top_k_respected(self, patch_model):
        patch_model(_FakeModel("[4, 3, 2, 1, 0]"))
        chunks = [_chunk(f"c{i}") for i in range(5)]
        result = await llm_rerank("q", chunks, top_k=2)
        assert len(result) == 2
        assert [c.name for c in result] == ["c4", "c3"]

    @pytest.mark.asyncio
    async def test_timeout_falls_back(self, patch_model):
        patch_model(_HangingModel())
        chunks = [_chunk("a"), _chunk("b")]
        # Force a tiny timeout
        result = await llm_rerank("q", chunks, top_k=2, timeout=0.05)
        # Should return input order, not crash
        assert [c.name for c in result] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_model_error_falls_back(self, patch_model):
        patch_model(_ErrorModel())
        chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
        result = await llm_rerank("q", chunks, top_k=2)
        assert [c.name for c in result] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_garbage_output_falls_back(self, patch_model):
        patch_model(_FakeModel("Sorry, I cannot rank these."))
        chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
        result = await llm_rerank("q", chunks, top_k=3)
        # Should return input order via the parser's identity fallback
        assert [c.name for c in result] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        result = await llm_rerank("q", [], top_k=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_single_chunk_short_circuits(self, patch_model):
        """With one candidate, no LLM call should happen."""
        fake = _FakeModel("[0]")
        patch_model(fake)
        chunks = [_chunk("only")]
        result = await llm_rerank("q", chunks, top_k=5)
        assert [c.name for c in result] == ["only"]
        assert fake.calls == 0  # short-circuited before the LLM call
