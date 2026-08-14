from __future__ import annotations

import pytest

from app.agent.intent import (
    classify_complexity,
    detect_topic_switch,
    get_meta_answer,
    is_meta_question,
)


def test_meta_question_detection_distinguishes_agent_from_project_questions():
    assert is_meta_question("what model are you using?") is True
    assert is_meta_question("what does this project do?") is False


def test_meta_answer_reports_configured_model(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "default_model", "qwen-test")

    assert "qwen-test" in get_meta_answer("what model are you using?")


def test_topic_switch_requires_previous_context_and_explicit_signal():
    previous = [{"role": "assistant", "content": "JWT authentication"}]

    assert detect_topic_switch("now inspect the database", previous) is True
    assert detect_topic_switch("continue", previous) is False
    assert detect_topic_switch("now inspect the database", []) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("show config", "simple"),
        ("refactor across multiple services and add tests", "complex"),
    ],
)
def test_complexity_classification(text, expected):
    assert classify_complexity(text) == expected
