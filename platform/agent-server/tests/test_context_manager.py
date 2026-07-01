"""Tests for context window management — _summarize_tool_messages, _make_state_modifier."""

from langchain_core.messages import (
    HumanMessage, AIMessage, ToolMessage, SystemMessage,
)

from app.agent.graph import (
    _make_state_modifier,
    _smart_compress,
    _summarize_tool_messages,
)


class TestSummarizeToolMessages:
    def _make_tool_msg(self, content: str, call_id: str = "call_1", name: str = "file_read"):
        return ToolMessage(content=content, tool_call_id=call_id, name=name)

    def test_no_trimming_when_few_tools(self):
        msgs = [
            HumanMessage(content="hello"),
            self._make_tool_msg("result1", "c1"),
            self._make_tool_msg("result2", "c2"),
            AIMessage(content="done"),
        ]
        result = _summarize_tool_messages(msgs)
        assert len(result) == len(msgs)
        # Content unchanged
        assert result[1].content == "result1"

    def test_trims_old_tool_messages(self):
        msgs = [HumanMessage(content="start")]
        # Add 6 tool messages — first 2 should be trimmed
        for i in range(6):
            msgs.append(self._make_tool_msg("x" * 2000, f"call_{i}"))
        msgs.append(AIMessage(content="end"))

        result = _summarize_tool_messages(msgs)
        # Old tools (index 0,1) should be truncated, last 4 kept full
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 6  # same count, but content differs

        # First 2 should be shorter than original
        assert len(tool_msgs[0].content) < 2000
        assert "summarized" in tool_msgs[0].content.lower() or "..." in tool_msgs[0].content
        # Last 4 should be untouched
        assert len(tool_msgs[-1].content) == 2000

    def test_preserves_non_tool_messages(self):
        msgs = [
            HumanMessage(content="question"),
            AIMessage(content="thinking"),
            self._make_tool_msg("x" * 3000, "c1"),
            self._make_tool_msg("x" * 3000, "c2"),
            self._make_tool_msg("x" * 3000, "c3"),
            self._make_tool_msg("x" * 3000, "c4"),
            self._make_tool_msg("x" * 3000, "c5"),
        ]
        result = _summarize_tool_messages(msgs)
        assert result[0].content == "question"
        assert result[1].content == "thinking"

    def test_short_tool_messages_not_trimmed(self):
        msgs = [HumanMessage(content="q")]
        for i in range(6):
            msgs.append(self._make_tool_msg("short", f"c{i}"))
        result = _summarize_tool_messages(msgs)
        # All short — even old ones stay untouched
        for m in result:
            if isinstance(m, ToolMessage):
                assert m.content == "short"


class TestSmartCompress:
    def test_keeps_listing_boundaries_for_file_lists(self):
        listing = "\n".join(f"file_{i}" for i in range(30))

        result = _smart_compress(listing, 80, tool_name="file_list")

        assert "file_0" in result
        assert "file_9" in result
        assert "file_20" in result
        assert "file_29" in result
        assert "... (10 lines omitted) ..." in result
        assert "file_10" not in result
        assert "file_19" not in result

    def test_returns_empty_string_when_budget_is_zero(self):
        assert _smart_compress("content", 0, tool_name="file_search") == ""


class TestStateModifier:
    def test_prepends_system_prompt(self):
        modifier = _make_state_modifier("You are helpful.")
        state = {"messages": [HumanMessage(content="hi")]}
        result = modifier(state)
        assert isinstance(result[0], SystemMessage)
        assert result[0].content.startswith("You are helpful.")
        assert result[1].content == "hi"

    def test_drops_old_messages_when_too_long(self):
        modifier = _make_state_modifier("sys")
        # Create messages totaling >20K chars
        msgs = []
        for i in range(30):
            msgs.append(HumanMessage(content=f"message {'x' * 1000} {i}"))
        state = {"messages": msgs}
        result = modifier(state)
        total_chars = sum(len(m.content) for m in result if not isinstance(m, SystemMessage))
        assert total_chars <= 25_000  # should be trimmed
        # Should keep recent messages
        assert "29" in result[-1].content  # last message preserved
