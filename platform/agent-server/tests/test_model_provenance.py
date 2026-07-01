from __future__ import annotations

from app.events.model_provenance import (
    build_feature_flags_snapshot,
    current_trace_id,
    infer_provider,
    make_model_call_event,
    validate_model_call_event,
)


def test_infer_provider():
    assert infer_provider("openai/gpt-4o") == "openai"
    assert infer_provider("gpt-4o") == "openai"
    assert infer_provider("anthropic/claude-sonnet") == "anthropic"
    assert infer_provider("claude-3.5-sonnet") == "anthropic"
    assert infer_provider("qwen2.5:7b") == "ollama"


def test_feature_flag_snapshot_has_core_keys():
    flags = build_feature_flags_snapshot()
    assert "agent_graph_version" in flags
    assert "prompt_version_default" in flags
    assert "direct_tool_routing_enabled" in flags
    assert "reflexion_enabled" in flags
    assert "subagent_verifier_enabled" in flags


def test_make_model_call_event_shape():
    event = make_model_call_event(
        run_id="run-1",
        request_id="req-1",
        trace_id=None,
        tenant_id="tenant-a",
        session_id="tenant-a:session-1",
        runtime="python-v2",
        call_site="chat._stream_agent_response.llm_call_1",
        provider="ollama",
        model="qwen2.5:7b",
        temperature=0.0,
        max_tokens=1024,
        prompt_id="coding-agent.system",
        prompt_version="v2",
        prompt_hash="sha256:" + ("a" * 64),
        feature_flags={"direct_tool_routing_enabled": False},
        prompt_tokens=123,
        completion_tokens=45,
        duration_ms=678,
        status="ok",
    )

    assert event["event_id"].startswith("mcall-")
    assert event["run_id"] == "run-1"
    assert event["request_id"] == "req-1"
    assert event["tenant_id"] == "tenant-a"
    assert event["prompt_version"] == "v2"
    assert event["prompt_hash"].startswith("sha256:")
    assert event["prompt_tokens"] == 123
    assert event["completion_tokens"] == 45
    assert event["duration_ms"] == 678
    assert event["status"] == "ok"


def test_current_trace_id_best_effort():
    # Should not throw even when tracing isn't configured.
    trace_id = current_trace_id()
    assert trace_id is None or isinstance(trace_id, str)


def test_model_call_event_passes_schema_validation():
    event = make_model_call_event(
        run_id="run-1",
        request_id="req-1",
        trace_id=None,
        tenant_id="tenant-a",
        session_id="tenant-a:session-1",
        runtime="python-v2",
        call_site="chat._stream_agent_response.llm_call_1",
        provider="ollama",
        model="qwen2.5:7b",
        temperature=0.0,
        max_tokens=1024,
        prompt_id="coding-agent.system",
        prompt_version="v2",
        prompt_hash="sha256:" + ("a" * 64),
        feature_flags={"direct_tool_routing_enabled": False},
        prompt_tokens=123,
        completion_tokens=45,
        duration_ms=678,
        status="ok",
    )

    is_valid, error = validate_model_call_event(event)
    assert is_valid, error


def test_invalid_prompt_hash_fails_schema_validation():
    event = make_model_call_event(
        run_id="run-1",
        request_id="req-1",
        trace_id=None,
        tenant_id="tenant-a",
        session_id="tenant-a:session-1",
        runtime="python-v2",
        call_site="chat._stream_agent_response.llm_call_1",
        provider="ollama",
        model="qwen2.5:7b",
        temperature=0.0,
        max_tokens=1024,
        prompt_id="coding-agent.system",
        prompt_version="v2",
        prompt_hash="not-a-sha256",
        feature_flags={"direct_tool_routing_enabled": False},
        prompt_tokens=123,
        completion_tokens=45,
        duration_ms=678,
        status="ok",
    )

    is_valid, error = validate_model_call_event(event)
    assert not is_valid
    assert error is not None
    assert "prompt_hash" in error


