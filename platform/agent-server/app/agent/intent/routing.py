"""Conversation topic and request-complexity routing heuristics."""

from __future__ import annotations

import re

TOPIC_SWITCH_SIGNALS = [
    r"^(now|next|also|btw|by the way|another|different|new|switch)",
    r"^(can you|could you|please) (?!continue|keep|finish|complete)",
    r"^(forget|ignore|stop|cancel|never ?mind)",
]


def detect_topic_switch(current_message: str, previous_messages: list[dict]) -> bool:
    """Detect an explicit topic switch when prior conversation exists."""
    if not previous_messages:
        return False
    normalized = current_message.lower().strip()
    return len(normalized.split()) <= 8 and any(
        re.search(pattern, normalized) for pattern in TOPIC_SWITCH_SIGNALS
    )


def classify_complexity(text: str) -> str:
    """Classify a query as simple or complex for evaluation/model routing."""
    normalized = text.lower()
    complex_patterns = [
        r"(fix|debug|refactor|redesign|architect|migrate|implement|build)",
        r"(across|multiple|all) (files|modules|services|components)",
        r"(why|how) .{50,}",
        r"(test|spec|coverage|benchmark|performance)",
        r"(deploy|ci|cd|docker|kubernetes)",
    ]
    simple_patterns = [
        r"^(what|where|which|show|list|find|read|cat|grep) ",
        r"^(explain|summarize|describe) .{0,50}$",
        r"(status|version|config|health)",
    ]
    complex_score = sum(
        1 for pattern in complex_patterns if re.search(pattern, normalized)
    )
    simple_score = sum(
        1 for pattern in simple_patterns if re.search(pattern, normalized)
    )
    return "complex" if complex_score > simple_score else "simple"
