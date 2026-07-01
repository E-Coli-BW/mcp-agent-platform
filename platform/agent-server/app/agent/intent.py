"""Intent & topic detection — preprocesses user input before the agent loop.

Handles:
1. Topic switch detection (new topic → reset agent context)
2. Meta-questions (about the agent itself → answer directly, no tools)
3. Multi-intent decomposition (sparse input → structured tasks)
"""

import re

# Meta-questions that should be answered directly without tools
META_PATTERNS = [
    r"what (model|llm|ai) (are you|do you) us",
    r"which (model|llm|provider)",
    r"do you support (agent|agentic|autonomous)",
    r"what (tools|capabilities) do you have",
    r"how (do you|does this) work",
    r"what is your (name|version)",
    r"(help|usage|commands)",
    r"(who|what) are you",
]

# Topic switch indicators — suggests user changed subject
TOPIC_SWITCH_SIGNALS = [
    r"^(now|next|also|btw|by the way|another|different|new|switch)",
    r"^(can you|could you|please) (?!continue|keep|finish|complete)",
    # Removed: r"^(what|how|why|when|where|who) " — too aggressive,
    # triggered on legitimate follow-ups like "What does this function do?"
    r"^(forget|ignore|stop|cancel|never ?mind)",
]


def is_meta_question(text: str) -> bool:
    """Check if the user is asking about the agent itself (not a coding task)."""
    lower = text.lower().strip()
    return any(re.search(p, lower) for p in META_PATTERNS)


def get_meta_answer(text: str) -> str:
    """Answer meta-questions directly without using tools."""
    lower = text.lower().strip()

    if re.search(r"what (model|llm)", lower):
        from app.config import settings
        return (
            f"I'm using **{settings.default_model}** via Ollama (local). "
            f"I also support OpenAI (gpt-4o) and Anthropic (Claude) — "
            f"just set the API key in the config."
        )

    if re.search(r"do you support (agent|agentic|autonomous)", lower):
        return (
            "Currently I'm in **ask mode** — I search code, read files, and answer questions. "
            "For full **agent mode** (autonomous code editing), I'd need additional tools like "
            "`file_write`, `file_edit`, and `git_commit`. The architecture supports it — "
            "it's just adding more tools to the LangGraph agent, no core changes needed."
        )

    if re.search(r"what (tools|capabilities)", lower):
        from app.tools.definitions import get_tools
        tool_count = len(get_tools())
        return (
            f"I have {tool_count} tools:\n"
            "- `rag_search` — semantic code search (embedding + BM25)\n"
            "- `memory_search/set/context` — persistent cross-session memory\n"
            "- `file_search/read/list` — file system operations\n"
            "- `code_run/shell` — sandboxed code execution\n"
        )

    if re.search(r"(who|what) are you", lower):
        return (
            "I'm a coding agent built with LangGraph (Python) + Java MCP tool backends. "
            "I can search your codebase semantically, read files, execute code, "
            "and remember context across sessions."
        )

    return ""


def detect_topic_switch(current_message: str, previous_messages: list[dict]) -> bool:
    """Detect if the user is switching to a new topic.
    
    Heuristics:
    1. Message starts with topic-switch signals
    2. Message is a question when previous context was a task
    3. Message has no semantic overlap with recent context
    """
    if not previous_messages:
        return False

    lower = current_message.lower().strip()

    # Short messages are often topic switches
    if len(lower.split()) <= 8 and any(re.search(p, lower) for p in TOPIC_SWITCH_SIGNALS):
        return True

    # Removed: "long reply + short follow-up → topic switch" heuristic
    # This caused false positives: after a long code analysis, "fix the bug"
    # (short, <100 chars) would trigger topic reset, losing conversation context.

    return False


def classify_complexity(text: str) -> str:
    """Classify query as 'simple' or 'complex' for model routing.

    Simple: direct questions, single-file reads, status checks
    Complex: multi-file edits, debugging, architecture questions, refactoring
    """
    lower = text.lower()

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

    complex_score = sum(1 for p in complex_patterns if re.search(p, lower))
    simple_score = sum(1 for p in simple_patterns if re.search(p, lower))

    return "complex" if complex_score > simple_score else "simple"
