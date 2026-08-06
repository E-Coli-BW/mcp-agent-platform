from __future__ import annotations

from app.runtime.tool_invocation import (
    ToolRisk,
    classify_tool_risk,
    hash_arguments,
    make_tool_invocation_envelope,
)


def test_hash_arguments_is_stable_for_key_order():
    left = hash_arguments({"path": "a.py", "start_line": 1})
    right = hash_arguments({"start_line": 1, "path": "a.py"})

    assert left == right
    assert left.startswith("sha256:")


def test_tool_risk_classification():
    assert classify_tool_risk("file_read") == ToolRisk.READ
    assert classify_tool_risk("file_write") == ToolRisk.WRITE
    assert classify_tool_risk("code_shell") == ToolRisk.EXECUTE
    assert classify_tool_risk("spawn_subagent") == ToolRisk.META
    assert classify_tool_risk("plugin_custom") == ToolRisk.UNKNOWN


def test_make_tool_invocation_envelope_shape():
    envelope = make_tool_invocation_envelope(
        run_id="run-1",
        request_id="req-1",
        session_id="tenant:s1",
        tenant_id="tenant",
        step_seq=3,
        tool_name="file_write",
        arguments={"path": "a.py", "content": "print(1)"},
    )

    payload = envelope.to_event_payload()
    assert payload["invocation_id"] == "tinv-run-1-3"
    assert payload["run_id"] == "run-1"
    assert payload["tool_name"] == "file_write"
    assert payload["risk"] == "write"
    assert payload["args_hash"].startswith("sha256:")
    assert payload["idempotency_key"].startswith("tool:")


def test_idempotency_key_is_stable_for_same_step_with_different_args():
    first = make_tool_invocation_envelope(
        run_id="run-1",
        request_id="req-1",
        session_id="tenant:s1",
        tenant_id="tenant",
        step_seq=3,
        tool_name="file_write",
        arguments={"path": "a.py", "content": "one"},
    )
    second = make_tool_invocation_envelope(
        run_id="run-1",
        request_id="req-1",
        session_id="tenant:s1",
        tenant_id="tenant",
        step_seq=3,
        tool_name="file_write",
        arguments={"path": "a.py", "content": "two"},
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.args_hash != second.args_hash
