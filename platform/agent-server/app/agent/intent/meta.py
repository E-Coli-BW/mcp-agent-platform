"""Meta-question detection and direct responses outside the Agent loop."""

from __future__ import annotations

import re

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


def is_meta_question(text: str) -> bool:
    """Return whether the user is asking about the Agent itself."""
    normalized = text.lower().strip()
    return any(re.search(pattern, normalized) for pattern in META_PATTERNS)


def get_meta_answer(text: str) -> str:
    """Render a direct answer for a recognized meta-question."""
    normalized = text.lower().strip()

    if re.search(r"what (model|llm)", normalized):
        from app.config import settings

        return (
            f"I'm using **{settings.default_model}** via Ollama (local). "
            "I also support OpenAI (gpt-4o) and Anthropic (Claude) — "
            "just set the API key in the config."
        )

    if re.search(r"do you support (agent|agentic|autonomous)", normalized):
        return (
            "Currently I'm in **ask mode** — I search code, read files, and answer "
            "questions. For full **agent mode** (autonomous code editing), I'd need "
            "additional tools like `file_write`, `file_edit`, and `git_commit`. The "
            "architecture supports it — it's just adding more tools to the Agent."
        )

    if re.search(r"what (tools|capabilities)", normalized):
        from app.tools.definitions import get_tools

        return (
            f"I have {len(get_tools())} tools:\n"
            "- `rag_search` — semantic code search (embedding + BM25)\n"
            "- `memory_search/set/context` — persistent cross-session memory\n"
            "- `file_search/read/list` — file system operations\n"
            "- `code_run/shell` — sandboxed code execution\n"
        )

    if re.search(r"(who|what) are you", normalized):
        return (
            "I'm a coding agent built with LangGraph (Python) + Java MCP tool "
            "backends. I can search your codebase semantically, read files, execute "
            "code, and remember context across sessions."
        )

    return ""
