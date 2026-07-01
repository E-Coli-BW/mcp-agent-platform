"""Pure-logic tests for scorers — no agent, no network."""
from __future__ import annotations

import pytest

from eval.case import Case, Expectations, Trajectory, ToolCall, parse_tool_markers
from eval.scorers import SCORERS
from eval.scorers.answer_grounded import answer_grounded
from eval.scorers.budgets import budgets
from eval.scorers.tool_selection import tool_selection
from eval.scorers.transport import transport


def _case(**exp) -> Case:
    return Case(id="t", description="", prompt="p", expected=Expectations(**exp))


def _ok_traj(**overrides) -> Trajectory:
    base = {"case_id": "t", "prompt": "p", "final_answer": "blue", "http_status": 200,
            "latency_ms": 100, "prompt_tokens": 50, "completion_tokens": 5,
            "tool_calls": [ToolCall(tool="memory_set"), ToolCall(tool="memory_search")]}
    base.update(overrides)
    return Trajectory(**base)


class TestTransport:
    def test_passes_when_http_200(self):
        s = transport(_case(), _ok_traj())
        assert s.passed and "200" in s.detail

    def test_fails_on_transport_error(self):
        s = transport(_case(), _ok_traj(error="connection refused"))
        assert not s.passed and "refused" in s.detail

    def test_fails_on_non_200(self):
        s = transport(_case(), _ok_traj(http_status=500))
        assert not s.passed and "500" in s.detail


class TestToolSelection:
    def test_none_when_no_expectations(self):
        assert tool_selection(_case(), _ok_traj()) is None

    def test_passes_when_all_required_present(self):
        c = _case(tools_called=["memory_set", "memory_search"])
        s = tool_selection(c, _ok_traj())
        assert s.passed

    def test_fails_when_required_missing(self):
        c = _case(tools_called=["memory_set", "code_run"])
        s = tool_selection(c, _ok_traj())
        assert not s.passed and "code_run" in s.detail

    def test_forbidden_tool_caught(self):
        c = _case(tools_forbidden=["memory_set"])
        s = tool_selection(c, _ok_traj())
        assert not s.passed and "forbidden" in s.detail

    def test_ceiling_caught_runaway(self):
        many = Trajectory(case_id="t", prompt="p", final_answer="",
                          http_status=200,
                          tool_calls=[ToolCall(tool="memory_search")] * 20)
        c = _case(tools_called_max=5)
        s = tool_selection(c, many)
        assert not s.passed and "exceeds" in s.detail

    def test_floor_caught_silent_agent(self):
        c = _case(tools_called_min=1)
        s = tool_selection(c, Trajectory(case_id="t", prompt="p", final_answer="hi",
                                          http_status=200))
        assert not s.passed


class TestAnswerGrounded:
    def test_substring_match_case_insensitive(self):
        c = _case(final_must_contain=["BLUE"])
        s = answer_grounded(c, _ok_traj(final_answer="The color is blue, definitely."))
        assert s.passed

    def test_missing_substring_fails(self):
        c = _case(final_must_contain=["red"])
        s = answer_grounded(c, _ok_traj(final_answer="The color is blue."))
        assert not s.passed and "missing" in s.detail.lower()

    def test_forbidden_substring_caught(self):
        c = _case(final_must_not_contain=["error"])
        s = answer_grounded(c, _ok_traj(final_answer="Got an Error trying to read."))
        assert not s.passed


class TestBudgets:
    def test_none_when_no_budget(self):
        assert budgets(_case(), _ok_traj()) is None

    def test_passes_within_budget(self):
        c = _case(max_latency_ms=1000, max_prompt_tokens=100)
        s = budgets(c, _ok_traj())
        assert s.passed

    def test_fails_over_latency(self):
        c = _case(max_latency_ms=50)
        s = budgets(c, _ok_traj(latency_ms=500))
        assert not s.passed and "latency" in s.detail.lower()


class TestToolMarkerParsing:
    def test_parses_well_formed_marker(self):
        content = '<!-- TOOL:{"tool": "memory_set", "action": "start"} -->\n🔧 Using memory_set...'
        markers = parse_tool_markers(content)
        assert len(markers) == 1
        assert markers[0]["tool"] == "memory_set"

    def test_skips_malformed_marker(self):
        content = '<!-- TOOL:{not json} -->\n<!-- TOOL:{"tool": "ok", "action": "start"} -->'
        markers = parse_tool_markers(content)
        assert len(markers) == 1
        assert markers[0]["tool"] == "ok"


def test_scorers_are_registered():
    # Catches "added a new scorer module but forgot to import in __init__".
    names = {s.__name__ for s in SCORERS}
    assert {"transport", "budgets", "tool_selection", "answer_grounded"} <= names
