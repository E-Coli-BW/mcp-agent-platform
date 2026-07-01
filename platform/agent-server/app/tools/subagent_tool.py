"""LangChain @tool wrapper for spawn_subagent.

Kept in a separate file from app.agent.subagent because:
- The engine (app.agent.subagent) is pure Python and trivially unit-testable
  without LangChain in the test path.
- This wrapper has LangChain schema decorators that pull in heavy imports
  and constrain the function signature in ways tests find awkward.

This file is what app.tools.definitions imports to register the tool. The
LLM sees ONLY this wrapper's signature + docstring — that's the "API" the
LLM is programming against.
"""
from __future__ import annotations

import logging

from langchain_core.tools import tool

from app.agent.subagent import spawn_subagent as _spawn

logger = logging.getLogger(__name__)


@tool
async def spawn_subagent(
    role: str,
    brief: str,
    allowed_tools: list[str] | None = None,
    max_tool_calls: int = 8,
) -> str:
    """Delegate a focused subtask to a specialist child agent.

    Use this for **embarrassingly parallel** work or when you need a
    narrowly-scoped agent with a SUBSET of your tools — e.g.,
      • summarize 5 files in parallel, one subagent per file
      • use a sandboxed reasoner without your write tools
      • get an independent second opinion on a draft

    DO NOT use this for:
      • Simple single-step tool calls — just call the tool yourself
      • Tasks where you need to keep the conversation context — subagents
        cannot see your history and you cannot see their tool trace
      • Recursive decomposition deeper than 2-3 levels — quality degrades
        sharply with depth (telephone effect)

    The subagent runs with a NARROWED tool set (must be a subset of what
    you have), a hard tool-call ceiling, and a token budget shared across
    the whole request. If you exceed any of those limits, the spawn
    returns an error and you should handle it gracefully.

    Args:
        role: Short label for what the subagent is, e.g. "sql-specialist",
            "file-summarizer", "reviewer". Used only for logging — it is
            NOT a registered identity.
        brief: The COMPLETE task description for the subagent. Include
            everything it needs to know — file paths, the question to
            answer, the expected output format. The subagent has no
            access to your conversation history.
        allowed_tools: Which of YOUR tools the subagent may use. Defaults
            to none (pure LLM reasoning). Must be a subset of the tools
            you're allowed to delegate. Example: ["file_read",
            "rag_search"] for a code-reading specialist.
        max_tool_calls: Hard ceiling on the subagent's ReAct loop.
            Default 8 is enough for "read 3 files and summarize"; bump
            higher for compound tasks. Never higher than 20.

    Returns:
        A short formatted string starting with ✅ or ❌ summarizing the
        subagent's outcome, followed by its final answer. On policy
        rejection (e.g. depth limit) only the ❌ line is returned with
        an explanation — try a different approach.

    Examples:
        # Parallel file summaries
        result = await spawn_subagent(
            role="file-summarizer",
            brief="Read src/auth.py and src/db.py and tell me how the "
                  "two modules interact in one paragraph.",
            allowed_tools=["file_read"],
            max_tool_calls=4,
        )

        # Sandboxed reasoner — pure LLM, no tools, useful for a fresh
        # perspective on a draft you already wrote
        critique = await spawn_subagent(
            role="reviewer",
            brief="Critique this commit message for clarity: ...",
        )
    """
    tools = allowed_tools or []
    # Bound max_tool_calls — the LLM has been known to pick absurd values.
    bounded = max(1, min(int(max_tool_calls), 20))

    result = await _spawn(
        role=role,
        brief=brief,
        allowed_tools=tools,
        max_tool_calls=bounded,
    )
    return result.format_for_llm()
