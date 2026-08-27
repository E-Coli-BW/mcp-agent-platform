from eval.warm_cache import (
    compare_first_two_turns,
    summarize_model_calls,
    summarize_tool_calls,
)


def _model_event(*, prefill_tokens: int, prefill_ms: int, duration_ms: int):
    return {
        "event_type": "model_call_completed",
        "payload": {
            "prompt_tokens": prefill_tokens,
            "completion_tokens": 10,
            "duration_ms": duration_ms,
            "input_prefix_hash": "sha256:" + ("a" * 64),
            "input_prefix_chars": 500,
            "performance": {
                "prefill_tokens": prefill_tokens,
                "prefill_duration_ms": prefill_ms,
                "decode_tokens": 10,
                "decode_duration_ms": 100,
            },
        },
    }


def test_summarize_model_calls_aggregates_provider_metrics():
    summary = summarize_model_calls(
        [
            {"event_type": "status_thinking", "payload": {}},
            _model_event(prefill_tokens=1_000, prefill_ms=2_000, duration_ms=2_200),
            _model_event(prefill_tokens=200, prefill_ms=400, duration_ms=550),
        ]
    )

    assert summary["model_calls"] == 2
    assert summary["prefill_tokens"] == 1_200
    assert summary["prefill_duration_ms"] == 2_400
    assert summary["prefill_tokens_per_second"] == 500.0
    assert summary["decode_tokens_per_second"] == 100.0
    assert summary["model_duration_ms"] == 2_750


def test_compare_first_two_turns_reports_warm_to_cold_ratios():
    cold_summary = summarize_model_calls(
        [_model_event(prefill_tokens=1_000, prefill_ms=2_000, duration_ms=2_200)]
    )
    warm_summary = summarize_model_calls(
        [_model_event(prefill_tokens=250, prefill_ms=500, duration_ms=700)]
    )

    comparison = compare_first_two_turns(
        [
            {"http_latency_ms": 3_000, "model_summary": cold_summary},
            {"http_latency_ms": 1_500, "model_summary": warm_summary},
        ]
    )

    assert comparison == {
        "stable_prefix_match": True,
        "warm_to_cold_http_latency": 0.5,
        "warm_to_cold_total_prefill_duration": 0.25,
        "warm_to_cold_first_call_prefill_duration": 0.25,
        "warm_to_cold_first_call_prefill_tokens": 0.25,
    }


def test_summarize_tool_calls_classifies_visible_tool_errors():
    calls = summarize_tool_calls(
        [
            {
                "event_type": "tool_started",
                "payload": {"seq": 1, "tool": "file_read"},
            },
            {
                "event_type": "tool_completed",
                "payload": {
                    "seq": 1,
                    "tool": "file_read",
                    "output_preview": "File: README.md",
                },
            },
            {
                "event_type": "tool_completed",
                "payload": {
                    "seq": 2,
                    "tool": "file_search",
                    "output_preview": "❌ unavailable",
                },
            },
        ]
    )

    assert calls == [
        {
            "seq": 1,
            "tool": "file_read",
            "status": "ok",
            "output_chars": 15,
            "output_sha256": "bc61bfdda19067871842e9453c0fc6481aa00cafd09f2501272bc3cb1bd1912b",
        },
        {
            "seq": 2,
            "tool": "file_search",
            "status": "error",
            "output_chars": 13,
            "output_sha256": "e1ed88a7888af4fec0e2ef9d5b7b0aac86c59518160b8da85157c115499c8d2d",
        },
    ]