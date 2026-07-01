"""Tests for the LLM-judge scorer — mock the HTTP call.

These tests do NOT hit a live LLM; we monkeypatch httpx.post so the
suite stays deterministic and free. There's a separate integration
test marked `live` for end-to-end validation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from eval.case import Case, Expectations, Trajectory
from eval.scorers.llm_judge import _aggregate, llm_judge


def _case(judge_cfg=None) -> Case:
    exp = Expectations(judge=judge_cfg) if judge_cfg else Expectations()
    return Case(id="t", description="", prompt="explain X", expected=exp)


def _traj(answer="A long, thoughtful explanation.") -> Trajectory:
    return Trajectory(
        case_id="t", prompt="explain X", final_answer=answer,
        http_status=200, latency_ms=100,
    )


# ── Skip behavior ────────────────────────────────────────────────────

class TestSkipBehavior:
    def test_returns_none_when_no_judge_config(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        assert llm_judge(_case(judge_cfg=None), _traj()) is None

    def test_returns_none_when_env_not_configured(self, monkeypatch):
        monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
        # Even with judge config in YAML, no env = soft-skip.
        assert llm_judge(_case(judge_cfg={"rubric": "x"}), _traj()) is None

    def test_returns_none_on_transport_error(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        bad = Trajectory(case_id="t", prompt="p", final_answer="", error="boom")
        assert llm_judge(_case(judge_cfg={"rubric": "x"}), bad) is None

    def test_fails_on_empty_final_answer(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        empty = Trajectory(case_id="t", prompt="p", final_answer="   ", http_status=200)
        s = llm_judge(_case(judge_cfg={"rubric": "x"}), empty)
        assert s is not None and not s.passed
        assert "empty" in s.detail.lower()


# ── Judge call mocking ───────────────────────────────────────────────

def _make_judge_response(scores: dict, overall: float, rationale: str = "fine"):
    """Build a fake httpx response matching OpenAI chat-completion shape."""
    fake = MagicMock()
    fake.raise_for_status = MagicMock()
    fake.json.return_value = {
        "choices": [{"message": {"content": json.dumps({
            "scores": scores,
            "rationale": rationale,
            "overall": overall,
        })}}]
    }
    return fake


class TestJudgeCall:
    def test_passes_when_median_above_min(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        # Mock 3 samples all returning overall=4.5 — median=4.5, passes min=4.
        responses = [
            _make_judge_response({"clarity": 5, "correctness": 4}, 4.5, "good"),
            _make_judge_response({"clarity": 4, "correctness": 5}, 4.5, "good"),
            _make_judge_response({"clarity": 5, "correctness": 4}, 4.5, "good"),
        ]
        call_count = {"n": 0}

        def fake_post(*args, **kwargs):
            r = responses[call_count["n"]]
            call_count["n"] += 1
            return r

        import httpx
        monkeypatch.setattr(httpx, "post", fake_post)

        s = llm_judge(_case(judge_cfg={"rubric": "x", "min_score": 4, "samples": 3}), _traj())
        assert s is not None
        assert s.passed, s.detail
        assert "overall=4.5" in s.detail
        assert call_count["n"] == 3              # actually sampled N times

    def test_fails_when_median_below_min(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        responses = [
            _make_judge_response({"clarity": 3}, 3.0, "meh"),
            _make_judge_response({"clarity": 2}, 2.0, "bad"),
            _make_judge_response({"clarity": 3}, 3.0, "meh"),
        ]
        idx = {"n": 0}

        def fake_post(*args, **kwargs):
            r = responses[idx["n"]]; idx["n"] += 1; return r

        import httpx
        monkeypatch.setattr(httpx, "post", fake_post)

        s = llm_judge(_case(judge_cfg={"rubric": "x", "min_score": 4, "samples": 3}), _traj())
        assert s is not None and not s.passed
        assert "overall=3.0" in s.detail        # median of [3,2,3] = 3.0

    def test_fails_when_all_samples_unparseable(self, monkeypatch):
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "openai/gpt-4o")
        bad = MagicMock()
        bad.raise_for_status = MagicMock()
        bad.json.return_value = {"choices": [{"message": {"content": "not json at all"}}]}

        import httpx
        monkeypatch.setattr(httpx, "post", lambda *a, **k: bad)

        s = llm_judge(_case(judge_cfg={"rubric": "x", "samples": 2}), _traj())
        assert s is not None and not s.passed
        assert "parseable" in s.detail


# ── Aggregation ──────────────────────────────────────────────────────

class TestAggregation:
    def test_median_overall(self):
        samples = [{"overall": 4.0, "scores": {}}, {"overall": 5.0, "scores": {}},
                   {"overall": 3.0, "scores": {}}]
        med, per_crit, _ = _aggregate(samples)
        assert med == 4.0                       # median of [4,5,3]

    def test_per_criterion_median(self):
        samples = [
            {"overall": 4.0, "scores": {"clarity": 4, "correctness": 5}, "rationale": ""},
            {"overall": 5.0, "scores": {"clarity": 5, "correctness": 5}, "rationale": ""},
            {"overall": 3.0, "scores": {"clarity": 3, "correctness": 4}, "rationale": ""},
        ]
        _, per_crit, _ = _aggregate(samples)
        assert per_crit["clarity"] == 4.0       # median [4,5,3]
        assert per_crit["correctness"] == 5.0   # median [5,5,4]

    def test_handles_missing_criteria(self):
        # Sample 2 missing 'tone' — should not crash
        samples = [
            {"overall": 4.0, "scores": {"clarity": 4, "tone": 5}},
            {"overall": 3.0, "scores": {"clarity": 3}},
        ]
        _, per_crit, _ = _aggregate(samples)
        assert per_crit["clarity"] == 3.5
        assert per_crit["tone"] == 5.0          # only 1 vote, still works
