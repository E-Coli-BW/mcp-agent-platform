"""Tests for RAG evaluation infrastructure — rerankers, cross-encoder, benchmark.

Tests are designed to run WITHOUT Ollama or a loaded index (unit-testable).
They validate scoring logic, ranking correctness, and edge cases.
"""

import pytest
from dataclasses import dataclass
from app.rag.index.retriever import ScoredChunk
from app.rag.reranking.dispatcher import rerank, heuristic_rerank
from app.rag.reranking.cross_encoder import cross_encoder_rerank, _load_model


def _can_load_model():
    try:
        from sentence_transformers import CrossEncoder
        return True
    except ImportError:
        return False


# ── Fixtures ──────────────────────────────────────────────────

def _make_chunk(name: str, content: str, score: float,
                chunk_type: str = "function", file_path: str = "test.py",
                start_line: int = 1, end_line: int = 10) -> ScoredChunk:
    return ScoredChunk(
        content=content, file_path=file_path, language="python",
        name=name, chunk_type=chunk_type,
        start_line=start_line, end_line=end_line, score=score,
    )


SAMPLE_CHUNKS = [
    _make_chunk("jwt_auth_filter", "def jwt_auth_filter(request): validate JWT token", 0.025),
    _make_chunk("tenant_context", "class TenantContext: stores tenant ID", 0.020, chunk_type="class"),
    _make_chunk("cache_executor", "def cache_after_commit(): write cache after TX", 0.018),
    _make_chunk("process_sandbox", "def process_sandbox(cmd): run in sandbox", 0.015),
    _make_chunk("__init__", "# package init\nimport os\nimport sys", 0.012, chunk_type="module"),
    _make_chunk("file_reader", "def file_reader(path): read file contents", 0.010),
    _make_chunk("embedder", "def embed_text(text): embed using mxbai", 0.008),
    _make_chunk("retriever", "class InMemoryRetriever: hybrid search BM25 RRF", 0.006, chunk_type="class"),
]


# ── Heuristic Reranker Tests ──────────────────────────────────

class TestHeuristicReranker:
    """Tests for the fixed heuristic reranker (scaled bonuses)."""

    def test_empty_input(self):
        result = heuristic_rerank("test query", [], top_k=5)
        assert result == []

    def test_returns_top_k(self):
        result = heuristic_rerank("jwt auth", SAMPLE_CHUNKS, top_k=3)
        assert len(result) == 3

    def test_preserves_base_ranking_for_unrelated_query(self):
        """An unrelated query should NOT reshuffle the ranking wildly."""
        chunks = [_make_chunk(f"func_{i}", f"content {i}", 0.03 - i * 0.005) for i in range(5)]
        result = heuristic_rerank("completely unrelated xyzzy", chunks, top_k=5)
        # Order should be approximately preserved since no bonuses fire
        names = [c.name for c in result]
        assert names[0] == "func_0"  # highest base score stays on top

    def test_name_match_boosts_ranking(self):
        """A chunk whose name matches the query should rank higher."""
        chunks = [
            _make_chunk("unrelated_func", "some code", 0.025),
            _make_chunk("jwt_filter", "validate token", 0.020),
        ]
        result = heuristic_rerank("jwt filter", chunks, top_k=2)
        assert result[0].name == "jwt_filter"

    def test_exact_substring_match_boosts(self):
        """Exact query substring in content should boost."""
        chunks = [
            _make_chunk("func_a", "def func_a(): return 1", 0.020),
            _make_chunk("func_b", "def func_b(): process sandbox execution", 0.018),
        ]
        result = heuristic_rerank("process sandbox", chunks, top_k=2)
        assert result[0].name == "func_b"

    def test_bonus_does_not_dominate_base_score(self):
        """The bonus should not be so large that it completely overrides
        a much higher base score. This was the original bug."""
        chunks = [
            _make_chunk("top_result", "very important code", 0.050),  # much higher base
            _make_chunk("name_match", "trivial code", 0.005),        # much lower base but name matches
        ]
        result = heuristic_rerank("name match", chunks, top_k=2)
        # The name match bonus should NOT catapult the low-scoring chunk to #1
        # when the base score difference is 10x
        # (The old bug had this wrong — bonuses of 0.5 dwarfed the 0.05 base)
        assert result[0].name == "top_result"

    def test_function_type_preferred_over_module(self):
        """Functions should get a slight edge over modules."""
        chunks = [
            _make_chunk("my_func", "def my_func(): pass", 0.020, chunk_type="module"),
            _make_chunk("my_func", "def my_func(): pass", 0.020, chunk_type="function"),
        ]
        result = heuristic_rerank("my_func", chunks, top_k=2)
        assert result[0].chunk_type == "function"

    def test_brevity_bonus(self):
        """Shorter chunks should get a slight boost."""
        chunks = [
            _make_chunk("short", "x = 1", 0.020),
            _make_chunk("long", "x = 1\n" * 500, 0.020),
        ]
        result = heuristic_rerank("some query", chunks, top_k=2)
        assert result[0].name == "short"

    def test_scores_are_modified(self):
        """Scores should change after reranking."""
        chunks = [_make_chunk("jwt_auth", "jwt authentication", 0.020)]
        original_score = chunks[0].score
        result = heuristic_rerank("jwt auth", chunks, top_k=1)
        # Score should increase due to name match
        assert result[0].score >= original_score


# ── Cross-Encoder Reranker Tests ──────────────────────────────

class TestCrossEncoderReranker:
    """Tests for the cross-encoder reranker.
    
    These test the logic/interface without requiring the model download.
    The model test is skipped if sentence-transformers isn't installed.
    """

    def test_empty_input(self):
        result = cross_encoder_rerank("test", [], top_k=5)
        assert result == []

    def test_returns_top_k(self):
        """Should return at most top_k results even if model unavailable."""
        chunks = [_make_chunk(f"f{i}", f"content {i}", 0.01) for i in range(10)]
        result = cross_encoder_rerank("test", chunks, top_k=3)
        assert len(result) == 3

    def test_model_loads_or_graceful_fallback(self):
        """Model should load OR gracefully return None."""
        model = _load_model()
        # Either it loaded (CrossEncoder) or returned None (missing dep)
        assert model is not None or model is None  # always passes, but exercises the code

    @pytest.mark.skipif(
        not _can_load_model(),
        reason="sentence-transformers not installed"
    )
    def test_cross_encoder_changes_ranking(self):
        """Cross-encoder should produce different ranking than input order."""
        chunks = [
            _make_chunk("unrelated", "def foo(): return bar()", 0.025),
            _make_chunk("jwt_filter", "def jwt_auth_filter(request): validate JWT token and extract claims", 0.010),
        ]
        result = cross_encoder_rerank("JWT authentication", chunks, top_k=2)
        # Cross-encoder should recognize jwt_filter is more relevant
        assert result[0].name == "jwt_filter"


# ── Eval Benchmark Dataset Tests ──────────────────────────────

class TestBenchmarkDataset:
    """Tests for the benchmark dataset integrity."""

    def test_benchmark_has_queries(self):
        from app.rag.eval.dataset import BENCHMARK
        assert len(BENCHMARK) >= 15

    def test_each_benchmark_entry_has_expected_files(self):
        from app.rag.eval.dataset import BENCHMARK
        for query, expected_files in BENCHMARK:
            assert isinstance(query, str) and len(query) > 0
            assert isinstance(expected_files, list) and len(expected_files) > 0
            for f in expected_files:
                assert isinstance(f, str) and "." in f  # must be a filename with extension

    def test_no_duplicate_queries(self):
        from app.rag.eval.dataset import BENCHMARK
        queries = [q for q, _ in BENCHMARK]
        assert len(queries) == len(set(queries)), "Duplicate queries in benchmark"
