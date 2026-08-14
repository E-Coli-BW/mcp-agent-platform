from __future__ import annotations

import pytest

from app.agent import classifier_eval


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
        classifier_eval.httpx,
        "AsyncClient",
        lambda **_kwargs: FailingAsyncClient(),
    )

    assert await classifier_eval.llm_intent("what model are you using?") == "meta"
    assert await classifier_eval.llm_topic_switch("continue", "JWT auth") == "continue"


def test_direct_routing_evaluation_reports_labeled_set_metrics():
    result = classifier_eval.evaluate_direct_routing_classifier()

    assert result.task == "direct_routing"
    assert result.total == len(classifier_eval.DIRECT_ROUTING_TEST_CASES)
    assert result.correct + len(result.errors) == result.total
    assert 0 <= result.accuracy <= 100
