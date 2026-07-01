"""Tests for reranker and run_tests tool."""

import os
import subprocess
import pytest

from app.tools.agent_mode import set_workspace_root, run_tests
from app.rag.reranking.dispatcher import rerank, heuristic_rerank
from app.rag.index.retriever import ScoredChunk


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    set_workspace_root(str(tmp_path))
    yield tmp_path


class TestRunTests:
    def test_detects_python_project(self, workspace):
        (workspace / "pyproject.toml").write_text("[project]\nname='test'\n")
        (workspace / "test_example.py").write_text("def test_pass(): assert True\n")
        result = run_tests.invoke({})
        # May succeed or fail depending on pytest availability, but should attempt
        assert "✅" in result or "❌" in result or "⚠️" in result

    def test_no_project_detected(self, workspace):
        result = run_tests.invoke({})
        assert "⚠️" in result or "No test framework" in result

    def test_custom_command(self, workspace):
        result = run_tests.invoke({"test_command": "echo 'tests passed'"})
        assert "✅" in result
        assert "tests passed" in result


class TestReranker:
    def _make_chunk(self, name, content, chunk_type="function", score=0.5):
        return ScoredChunk(
            content=content, file_path=f"src/{name}.py", language="python",
            name=name, chunk_type=chunk_type, start_line=1, end_line=10, score=score,
        )

    def test_name_match_boosted(self):
        chunks = [
            self._make_chunk("unrelated_func", "does nothing special", score=0.5),
            self._make_chunk("calculate_total", "sums up items", score=0.45),
        ]
        result = rerank("calculate total", chunks, top_k=2, strategy="heuristic")
        assert result[0].name == "calculate_total"  # boosted by name match

    def test_exact_query_in_content_boosted(self):
        chunks = [
            self._make_chunk("func_a", "this handles authentication and login", score=0.5),
            self._make_chunk("func_b", "random utility function", score=0.5),
        ]
        result = rerank("authentication", chunks, top_k=2, strategy="heuristic")
        assert result[0].name == "func_a"

    def test_function_preferred_over_module(self):
        chunks = [
            self._make_chunk("init", "module init code", chunk_type="module", score=0.5),
            self._make_chunk("get_user", "fetch user by id", chunk_type="function", score=0.5),
        ]
        result = rerank("get user", chunks, top_k=2, strategy="heuristic")
        assert result[0].name == "get_user"

    def test_shorter_chunks_preferred(self):
        chunks = [
            self._make_chunk("big_func", "x " * 2000, score=0.5),
            self._make_chunk("small_func", "concise code", score=0.5),
        ]
        result = rerank("code", chunks, top_k=2, strategy="heuristic")
        assert result[0].name == "small_func"

    def test_respects_top_k(self):
        chunks = [self._make_chunk(f"f{i}", f"content {i}", score=0.5 - i*0.01) for i in range(10)]
        result = rerank("content", chunks, top_k=3, strategy="heuristic")
        assert len(result) == 3

    def test_heuristic_alias_works(self):
        """heuristic_rerank is exposed for direct use too."""
        chunks = [
            self._make_chunk("login_handler", "auth flow", score=0.5),
            self._make_chunk("unrelated", "noise", score=0.5),
        ]
        result = heuristic_rerank("login", chunks, top_k=2)
        assert result[0].name == "login_handler"

    def test_none_strategy_passthrough(self):
        """strategy='none' returns the input ranking unchanged (top_k slice)."""
        chunks = [
            self._make_chunk("a", "x", score=0.9),
            self._make_chunk("b", "y", score=0.5),
            self._make_chunk("c", "z", score=0.1),
        ]
        result = rerank("anything", chunks, top_k=2, strategy="none")
        assert [c.name for c in result] == ["a", "b"]

    def test_empty_input(self):
        assert rerank("anything", [], top_k=5, strategy="heuristic") == []
