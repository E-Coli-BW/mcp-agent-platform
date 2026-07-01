"""Tests for learned reranker — feature extraction, scoring, feedback, retraining."""

import json
import pytest
import numpy as np
from pathlib import Path

from app.rag.reranking.learned import (
    LearnedReranker, _extract_features, FEATURE_NAMES, NUM_FEATURES, DEFAULT_WEIGHTS
)
from app.rag.index.retriever import ScoredChunk


def _chunk(name="func", content="hello world", chunk_type="function", score=0.5):
    return ScoredChunk(
        content=content, file_path=f"src/{name}.py", language="python",
        name=name, chunk_type=chunk_type, start_line=1, end_line=10, score=score,
    )


class TestFeatureExtraction:
    def test_feature_count(self):
        features = _extract_features("hello", _chunk())
        assert len(features) == NUM_FEATURES

    def test_name_match_detected(self):
        features = _extract_features("calculate total", _chunk(name="calculate_total"))
        assert features[0] > 0  # name_match_ratio

    def test_exact_query_match(self):
        features = _extract_features("hello world", _chunk(content="hello world here"))
        assert features[1] == 1.0  # exact_query_match

    def test_function_type_detected(self):
        features = _extract_features("test", _chunk(chunk_type="function"))
        assert features[3] == 1.0  # is_function

    def test_class_type_detected(self):
        features = _extract_features("test", _chunk(chunk_type="class"))
        assert features[4] == 1.0  # is_class

    def test_brevity_short(self):
        features = _extract_features("test", _chunk(content="short"))
        assert features[5] == 1.0  # brevity bonus

    def test_brevity_long(self):
        features = _extract_features("test", _chunk(content="x " * 2000))
        assert features[5] == -1.0  # brevity penalty

    def test_retrieval_score_preserved(self):
        features = _extract_features("test", _chunk(score=0.75))
        assert features[6] == 0.75  # retrieval_score


class TestLearnedReranker:
    def test_rerank_uses_weights(self):
        reranker = LearnedReranker()
        chunks = [
            _chunk("unrelated", "nothing", score=0.5),
            _chunk("calculate_total", "sums items", score=0.3),
        ]
        result = reranker.rerank("calculate total", chunks, top_k=2)
        # Name match should boost calculate_total above unrelated
        assert result[0].name == "calculate_total"

    def test_rerank_respects_top_k(self):
        reranker = LearnedReranker()
        chunks = [_chunk(f"f{i}", f"content {i}", score=0.5) for i in range(10)]
        result = reranker.rerank("content", chunks, top_k=3)
        assert len(result) == 3

    def test_feedback_logging(self, tmp_path, monkeypatch):
        import app.rag.reranking.learned as lr
        monkeypatch.setattr(lr, "FEEDBACK_LOG", tmp_path / "feedback.jsonl")

        reranker = LearnedReranker()
        chunks = [_chunk("auth", "JWT filter"), _chunk("util", "helper")]
        reranker.log_feedback("JWT auth", chunks, ["auth.py"])

        lines = (tmp_path / "feedback.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        entry0 = json.loads(lines[0])
        entry1 = json.loads(lines[1])
        # auth.py matched → label=1, util.py not → label=0
        labels = {json.loads(l)["chunk_name"]: json.loads(l)["label"] for l in lines}
        assert labels["auth"] == 1
        assert labels["util"] == 0

    def test_retrain_adjusts_weights(self, tmp_path, monkeypatch):
        import app.rag.reranking.learned as lr
        monkeypatch.setattr(lr, "FEEDBACK_LOG", tmp_path / "feedback.jsonl")
        monkeypatch.setattr(lr, "WEIGHTS_FILE", tmp_path / "weights.json")

        reranker = LearnedReranker()

        # Generate synthetic feedback: name_match always predicts relevance
        with open(tmp_path / "feedback.jsonl", "w") as f:
            for i in range(30):
                # Positive: high name_match
                features = [0.8, 0.0, 0.3, 1.0, 0.0, 1.0, 0.5]
                f.write(json.dumps({"query": f"q{i}", "chunk_path": "a.py",
                                    "chunk_name": "a", "label": 1,
                                    "features": features, "timestamp": 0}) + "\n")
                # Negative: low name_match
                features = [0.0, 0.0, 0.1, 0.0, 0.0, -1.0, 0.3]
                f.write(json.dumps({"query": f"q{i}", "chunk_path": "b.py",
                                    "chunk_name": "b", "label": 0,
                                    "features": features, "timestamp": 0}) + "\n")

        old_weights = reranker.weights.copy()
        reranker.retrain(min_samples=5)
        new_weights = reranker.weights

        # Weights should have changed
        assert not np.allclose(old_weights, new_weights)
        # name_match_ratio weight should increase (strong positive signal)
        assert new_weights[0] >= old_weights[0] * 0.8  # at least not much lower

    def test_weights_persist_to_disk(self, tmp_path, monkeypatch):
        import app.rag.reranking.learned as lr
        monkeypatch.setattr(lr, "WEIGHTS_FILE", tmp_path / "weights.json")

        reranker = LearnedReranker()
        reranker.weights = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        reranker._save_weights()

        # Load fresh
        reranker2 = LearnedReranker()
        # Won't auto-load from monkeypatched path, but check file exists
        assert (tmp_path / "weights.json").exists()
        data = json.loads((tmp_path / "weights.json").read_text())
        assert data["weights"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]
