"""Stable intent preprocessing API."""

from app.agent.intent.meta import META_PATTERNS, get_meta_answer, is_meta_question
from app.agent.intent.routing import (
    TOPIC_SWITCH_SIGNALS,
    classify_complexity,
    detect_topic_switch,
)

__all__ = [
    "META_PATTERNS",
    "TOPIC_SWITCH_SIGNALS",
    "classify_complexity",
    "detect_topic_switch",
    "get_meta_answer",
    "is_meta_question",
]
