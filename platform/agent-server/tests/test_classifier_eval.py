from __future__ import annotations

import pytest

from app.agent.intent import evaluation


class FailingAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, *args, **kwargs):
        raise OSError("model unavailable")


@pytest.mark.asyncio
async def test_llm_classifiers_fall_back_to_heuristics(monkeypatch):
    monkeypatch.setattr(
        evaluation.httpx,
        "AsyncClient",
        lambda **_kwargs: FailingAsyncClient(),
    )

    assert await evaluation.llm_intent("what model are you using?") == "meta"
    assert await evaluation.llm_topic_switch("continue", "JWT auth") == "continue"


def test_direct_routing_evaluation_reports_labeled_set_metrics():
    result = evaluation.evaluate_direct_routing_classifier()

    assert result.task == "direct_routing"
    assert result.total == len(evaluation.DIRECT_ROUTING_TEST_CASES)
    assert result.correct + len(result.errors) == result.total
    assert 0 <= result.accuracy <= 100
