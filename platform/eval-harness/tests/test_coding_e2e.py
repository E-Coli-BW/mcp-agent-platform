from __future__ import annotations

import stat
from pathlib import Path

import pytest

import eval.coding_e2e as coding_e2e
from eval.case import ToolCall, Trajectory
from eval.coding_e2e import (
    CommandResult,
    capture_changes,
    create_workspace,
    load_suite,
    preflight_suite,
    remove_workspace,
    score_live_run,
    write_report,
)


def _passed_command() -> CommandResult:
    return CommandResult(("python", "-m", "unittest"), 0, 1, "OK")


def test_suite_defines_five_common_coding_scenarios():
    cases = load_suite()

    assert {case.id for case in cases} == {
        "order_service_architecture",
        "order_service_api_documentation",
        "order_service_business_flow",
        "order_service_improve_item_validation",
        "order_service_add_cancellation",
    }
    assert {case.kind for case in cases} == {"read_only", "mutation"}
    mutations = [case for case in cases if case.kind == "mutation"]
    assert all(case.expected.required_any_tools == ("file_edit", "file_write") for case in mutations)


def test_all_harness_controls_pass_without_live_agent():
    cases = load_suite()

    results = preflight_suite(cases)

    assert all(result.passed for result in results.values())
    for case in cases:
        result = results[case.id]
        assert result.public_test.passed
        if case.kind == "mutation":
            assert result.baseline_hidden_test is not None
            assert not result.baseline_hidden_test.passed
            assert result.control_public_test is not None
            assert result.control_public_test.passed
            assert result.control_hidden_test is not None
            assert result.control_hidden_test.passed


def test_git_capture_includes_tracked_and_untracked_changes():
    case = load_suite()[0]
    workspace, baseline = create_workspace(case)
    try:
        readme = workspace / "README.md"
        readme.write_text(readme.read_text() + "\nchanged\n")
        (workspace / "new-file.txt").write_text("new\n")

        changed, diff = capture_changes(workspace, baseline)

        assert changed == ["README.md", "new-file.txt"]
        assert "changed" in diff
        assert "new-file.txt" in diff
    finally:
        remove_workspace(workspace, keep=False)


def test_scoring_attributes_runtime_and_capability_separately(tmp_path):
    case = next(case for case in load_suite() if case.id == "order_service_architecture")
    preflight = preflight_suite([case])[case.id]
    runtime_events = [
        {
            "event_type": "run_started",
            "payload": {"workspace_root": str(tmp_path)},
        },
        {"event_type": "model_call_completed", "payload": {}},
    ]
    trajectory = Trajectory(
        case_id=case.id,
        prompt=case.prompt,
        final_answer=(
            "OrderAPI OrderService InMemoryOrderRepository EventPublisher application.py"
        ),
        tool_calls=[ToolCall("file_search"), ToolCall("file_read")],
        http_status=200,
        latency_ms=100,
        run_id="run-1",
        trace_source="runtime_events",
        runtime_events=runtime_events,
    )

    passed = score_live_run(
        case,
        0,
        preflight,
        trajectory,
        tmp_path,
        [],
        "",
        _passed_command(),
        None,
    )
    assert passed.passed
    assert passed.failure_layer == "none"

    trajectory.final_answer = "generic answer"
    capability = score_live_run(
        case,
        1,
        preflight,
        trajectory,
        tmp_path,
        [],
        "",
        _passed_command(),
        None,
    )
    assert not capability.passed
    assert capability.failure_layer == "capability_or_agent_policy"

    trajectory.error = "runtime failed [tool_protocol_error]: malformed"
    protocol = score_live_run(
        case,
        2,
        preflight,
        trajectory,
        tmp_path,
        [],
        "",
        _passed_command(),
        None,
    )
    assert protocol.failure_layer == "model_runtime_protocol"


def test_mutation_scoring_rejects_public_test_changes(tmp_path):
    case = next(case for case in load_suite() if case.kind == "mutation")
    preflight = preflight_suite([case])[case.id]
    trajectory = Trajectory(
        case_id=case.id,
        prompt=case.prompt,
        final_answer="implemented",
        tool_calls=[
            ToolCall("file_search"),
            ToolCall("file_read"),
            ToolCall("file_edit"),
        ],
        http_status=200,
        run_id="run-mutation",
        trace_source="runtime_events",
        runtime_events=[
            {"event_type": "run_started", "payload": {"workspace_root": str(tmp_path)}},
            {"event_type": "model_call_completed", "payload": {}},
        ],
    )

    result = score_live_run(
        case,
        0,
        preflight,
        trajectory,
        tmp_path,
        [*case.expected.required_changed_files, "tests/test_service.py"],
        "diff",
        _passed_command(),
        _passed_command(),
    )

    assert not result.passed
    assert any("protected public tests" in issue for issue in result.oracle_issues)


def test_report_files_are_private(tmp_path):
    output = tmp_path / "report"

    write_report(output, {"case_count": 0, "run_count": 0, "passed": 0, "failed": 0, "by_failure_layer": {}, "runs": []})

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "report.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "report.md").stat().st_mode) == 0o600


def test_suite_validation_rejects_string_command(tmp_path):
    suite = tmp_path / "invalid.yaml"
    suite.write_text(
        """
version: 1
defaults:
  fixture: order_service
  public_test: python -m unittest
cases:
  - id: invalid
    kind: read_only
    prompt: read it
"""
    )

    with pytest.raises(ValueError, match="public_test must be a list of strings"):
        load_suite(suite)


async def test_restore_failure_preserves_workspace(tmp_path, monkeypatch):
    case = next(case for case in load_suite() if case.kind == "read_only")
    preflight = preflight_suite([case])[case.id]
    previous_workspace = tmp_path / "previous"
    previous_workspace.mkdir()
    switch_calls: list[Path] = []

    async def current_workspace(*_args):
        return previous_workspace

    async def switch_workspace(_client, _url, _jwt, workspace):
        switch_calls.append(workspace)
        if len(switch_calls) == 2:
            raise RuntimeError("restore unavailable")

    async def run_one(*_args, **_kwargs):
        return Trajectory(
            case_id=case.id,
            prompt=case.prompt,
            final_answer=" ".join(case.expected.answer_contains),
            tool_calls=[ToolCall(tool) for tool in case.expected.required_tools],
            http_status=200,
            trace_source="runtime_events",
        )

    monkeypatch.setattr(coding_e2e, "_current_workspace", current_workspace)
    monkeypatch.setattr(coding_e2e, "_switch_workspace", switch_workspace)
    monkeypatch.setattr(coding_e2e, "_run_one", run_one)
    monkeypatch.setattr(coding_e2e, "capture_changes", lambda *_args: ([], ""))
    monkeypatch.setattr(coding_e2e, "run_command", lambda *_args: _passed_command())

    result = await coding_e2e.run_live_case(
        case,
        0,
        preflight,
        object(),
        "http://agent",
        "jwt",
        False,
    )
    preserved_workspace = Path(result.workspace or "")
    try:
        assert not result.passed
        assert result.failure_layer == "runtime"
        assert "workspace restore failed" in result.runtime_issues[-1]
        assert switch_calls[-1] == previous_workspace
        assert preserved_workspace.is_dir()
    finally:
        remove_workspace(preserved_workspace, keep=False)