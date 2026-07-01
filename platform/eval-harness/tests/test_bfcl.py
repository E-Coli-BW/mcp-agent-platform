"""Pure-logic tests for the BFCL scorer — no LLM, no network."""
from __future__ import annotations

import json

from suites.bfcl import BfclCase, _extract_tool_call, _values_match, score_case


def _case(expected_name="get_weather", expected_args=None, category="simple") -> BfclCase:
    return BfclCase(
        id="t", category=category, question="q", tools=[],
        expected_name=expected_name,
        expected_args=expected_args or {"location": "SF", "unit": "fahrenheit"},
    )


def _body_with_call(name: str, args: dict | str) -> dict:
    """Build a fake OpenAI response with one tool_call."""
    if isinstance(args, dict):
        args = json.dumps(args)
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": name, "arguments": args}}
    ]}}]}


def _body_no_call(prose: str = "I can't help with that.") -> dict:
    return {"choices": [{"message": {"content": prose, "tool_calls": []}}]}


# ── _values_match ────────────────────────────────────────────────────

class TestValuesMatch:
    def test_string_equal_case_insensitive(self):
        assert _values_match("Fahrenheit", "fahrenheit")

    def test_int_vs_float(self):
        assert _values_match(42, 42.0)

    def test_int_vs_string_does_not_match(self):
        assert not _values_match(42, "42")

    def test_bool_strict(self):
        assert _values_match(True, True)
        assert not _values_match(True, "true")

    def test_list_of_acceptable_values(self):
        # Ground truth says "any of these is OK"
        assert _values_match(["NYC", "New York", "New York City"], "New York")
        assert not _values_match(["NYC", "New York"], "Boston")

    def test_nested_list_value(self):
        # The model must call with the exact list contents
        assert _values_match([["a", "b", "c"]], ["a", "b", "c"])


# ── _extract_tool_call ───────────────────────────────────────────────

class TestExtractToolCall:
    def test_extracts_dict_args(self):
        n, a = _extract_tool_call(_body_with_call("foo", {"x": 1}))
        assert n == "foo" and a == {"x": 1}

    def test_extracts_string_args(self):
        n, a = _extract_tool_call(_body_with_call("foo", '{"x": 1}'))
        assert n == "foo" and a == {"x": 1}

    def test_returns_none_when_no_tool_call(self):
        assert _extract_tool_call(_body_no_call()) == (None, None)

    def test_handles_malformed_json_args(self):
        n, a = _extract_tool_call(_body_with_call("foo", "not json"))
        assert n == "foo" and a is None


# ── score_case ───────────────────────────────────────────────────────

class TestScoreCase:
    def test_correct_call_passes(self):
        body = _body_with_call("get_weather", {"location": "San Francisco", "unit": "fahrenheit"})
        r = score_case(
            _case(expected_args={
                "location": ["San Francisco", "SF"],
                "unit": "fahrenheit",
            }),
            body, latency_ms=42,
        )
        assert r.passed, r.detail
        assert r.sub_checks["name_matches"]
        assert r.sub_checks["all_arg_values_correct"]

    def test_wrong_function_name_fails(self):
        body = _body_with_call("other_fn", {})
        r = score_case(_case(), body, 0)
        assert not r.passed
        assert "wrong function" in r.detail
        assert not r.sub_checks["name_matches"]

    def test_missing_required_arg_fails(self):
        body = _body_with_call("get_weather", {"location": "SF"})  # missing unit
        r = score_case(_case(), body, 0)
        assert not r.passed
        assert "missing" in r.detail and "unit" in r.detail

    def test_wrong_value_fails(self):
        body = _body_with_call("get_weather", {"location": "SF", "unit": "celsius"})
        r = score_case(
            _case(expected_args={"location": "SF", "unit": "fahrenheit"}),
            body, 0,
        )
        assert not r.passed
        assert "unit" in r.detail

    def test_no_tool_call_fails(self):
        r = score_case(_case(), _body_no_call(), 0)
        assert not r.passed
        assert "no tool_call" in r.detail

    # ── "must not call" sentinel ─────────────────────────────────────

    def test_must_not_call_passes_on_no_call(self):
        r = score_case(_case(expected_name="__none__", expected_args={}), _body_no_call(), 0)
        assert r.passed
        assert "refrained" in r.detail

    def test_must_not_call_fails_when_model_calls(self):
        body = _body_with_call("get_weather", {"location": "SF", "unit": "celsius"})
        r = score_case(_case(expected_name="__none__", expected_args={}), body, 0)
        assert not r.passed
        assert "when no tool was appropriate" in r.detail
