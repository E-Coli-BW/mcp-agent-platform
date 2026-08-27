"""Fixture-backed coding-agent E2E suite with layered failure attribution."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from eval.case import Case, Trajectory
from eval.runner import (
    DEFAULT_AGENT_URL,
    DEFAULT_AUTH_URL,
    DEFAULT_JWT_FILE,
    HTTP_TIMEOUT,
    _ensure_jwt,
    _run_one,
    _write_private_text,
)

HARNESS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = HARNESS_ROOT / "suites" / "coding_e2e" / "cases.yaml"
DEFAULT_OUTPUT_ROOT = HARNESS_ROOT / "runs" / "coding-e2e"
FIXTURES_ROOT = HARNESS_ROOT / "fixtures"
SUITES_ROOT = HARNESS_ROOT / "suites" / "coding_e2e"
log = logging.getLogger("eval.coding_e2e")


@dataclass(frozen=True)
class CodingExpectations:
    answer_contains: tuple[str, ...] = ()
    answer_not_contains: tuple[str, ...] = ()
    answer_regex: str | None = None
    fixture_must_contain: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    required_any_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    max_tool_calls: int | None = None
    max_model_calls: int | None = None
    no_changes: bool = False
    required_changed_files: tuple[str, ...] = ()
    forbidden_changed_files: tuple[str, ...] = ()
    hidden_test: str | None = None
    control_overlay: str | None = None


@dataclass(frozen=True)
class CodingCase:
    id: str
    kind: str
    prompt: str
    fixture: str
    model: str
    public_test: tuple[str, ...]
    max_latency_ms: int
    tags: tuple[str, ...]
    expected: CodingExpectations


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    duration_ms: int
    output: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class PreflightResult:
    case_id: str
    passed: bool
    issues: tuple[str, ...]
    public_test: CommandResult
    baseline_hidden_test: CommandResult | None = None
    control_public_test: CommandResult | None = None
    control_hidden_test: CommandResult | None = None


@dataclass
class CodingRunResult:
    case_id: str
    run_index: int
    passed: bool
    failure_layer: str
    harness_issues: list[str] = field(default_factory=list)
    runtime_issues: list[str] = field(default_factory=list)
    oracle_issues: list[str] = field(default_factory=list)
    trajectory: Trajectory | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    public_test: CommandResult | None = None
    hidden_test: CommandResult | None = None
    workspace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        trajectory = self.trajectory
        return {
            "case_id": self.case_id,
            "run_index": self.run_index,
            "passed": self.passed,
            "failure_layer": self.failure_layer,
            "harness_issues": self.harness_issues,
            "runtime_issues": self.runtime_issues,
            "oracle_issues": self.oracle_issues,
            "run_id": trajectory.run_id if trajectory else None,
            "trace_source": trajectory.trace_source if trajectory else None,
            "latency_ms": trajectory.latency_ms if trajectory else None,
            "prompt_tokens": trajectory.prompt_tokens if trajectory else None,
            "completion_tokens": trajectory.completion_tokens if trajectory else None,
            "tool_calls": trajectory.tool_names if trajectory else [],
            "final_answer": trajectory.final_answer if trajectory else "",
            "changed_files": self.changed_files,
            "diff": self.diff,
            "public_test": asdict(self.public_test) if self.public_test else None,
            "hidden_test": asdict(self.hidden_test) if self.hidden_test else None,
            "workspace": self.workspace,
        }


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes suite root: {relative}")
    return candidate


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(value)


def _command_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    command = _string_tuple(value, field_name)
    if not command:
        raise ValueError(f"{field_name} must not be empty")
    return command


def _positive_int(value: Any, field_name: str, default: int) -> int:
    resolved = default if value is None else value
    if not isinstance(resolved, int) or isinstance(resolved, bool) or resolved < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return resolved


def load_suite(path: Path | str = DEFAULT_SUITE) -> list[CodingCase]:
    suite_path = Path(path)
    raw = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("coding E2E suite root must be a mapping")
    if raw.get("version") != 1:
        raise ValueError(f"unsupported coding E2E schema version: {raw.get('version')}")
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("coding E2E defaults must be a mapping")
    raw_cases = raw.get("cases") or []
    if not isinstance(raw_cases, list):
        raise ValueError("coding E2E cases must be a list")
    cases: list[CodingCase] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            raise ValueError("each coding E2E case must be a mapping")
        if "id" not in item or "kind" not in item or "prompt" not in item:
            raise ValueError("each coding E2E case requires id, kind, and prompt")
        expected = item.get("expected") or {}
        if not isinstance(expected, dict):
            raise ValueError(f"{item['id']}: expected must be a mapping")
        kind = str(item["kind"])
        if kind not in {"read_only", "mutation"}:
            raise ValueError(f"{item['id']}: unsupported kind {kind}")
        case = CodingCase(
            id=str(item["id"]),
            kind=kind,
            prompt=str(item["prompt"]).rstrip(),
            fixture=str(item.get("fixture", defaults.get("fixture", ""))),
            model=str(item.get("model", defaults.get("model", "coding-agent"))),
            public_test=_command_tuple(
                item.get("public_test", defaults.get("public_test")),
                f"{item['id']}.public_test",
            ),
            max_latency_ms=_positive_int(
                item.get("max_latency_ms", defaults.get("max_latency_ms")),
                f"{item['id']}.max_latency_ms",
                120_000,
            ),
            tags=_string_tuple(item.get("tags"), f"{item['id']}.tags"),
            expected=CodingExpectations(
                answer_contains=_string_tuple(
                    expected.get("answer_contains"), f"{item['id']}.answer_contains"
                ),
                answer_not_contains=_string_tuple(
                    expected.get("answer_not_contains"),
                    f"{item['id']}.answer_not_contains",
                ),
                answer_regex=expected.get("answer_regex"),
                fixture_must_contain=_string_tuple(
                    expected.get("fixture_must_contain"),
                    f"{item['id']}.fixture_must_contain",
                ),
                required_tools=_string_tuple(
                    expected.get("required_tools"), f"{item['id']}.required_tools"
                ),
                required_any_tools=_string_tuple(
                    expected.get("required_any_tools"),
                    f"{item['id']}.required_any_tools",
                ),
                forbidden_tools=_string_tuple(
                    expected.get("forbidden_tools"), f"{item['id']}.forbidden_tools"
                ),
                max_tool_calls=(
                    _positive_int(
                        expected.get("max_tool_calls"),
                        f"{item['id']}.max_tool_calls",
                        1,
                    )
                    if expected.get("max_tool_calls") is not None
                    else None
                ),
                max_model_calls=(
                    _positive_int(
                        expected.get("max_model_calls"),
                        f"{item['id']}.max_model_calls",
                        1,
                    )
                    if expected.get("max_model_calls") is not None
                    else None
                ),
                no_changes=bool(expected.get("no_changes", False)),
                required_changed_files=_string_tuple(
                    expected.get("required_changed_files"),
                    f"{item['id']}.required_changed_files",
                ),
                forbidden_changed_files=_string_tuple(
                    expected.get("forbidden_changed_files"),
                    f"{item['id']}.forbidden_changed_files",
                ),
                hidden_test=expected.get("hidden_test"),
                control_overlay=expected.get("control_overlay"),
            ),
        )
        fixture = _safe_child(FIXTURES_ROOT, case.fixture)
        if not fixture.is_dir():
            raise ValueError(f"{case.id}: fixture not found: {fixture}")
        if case.expected.answer_regex:
            try:
                re.compile(case.expected.answer_regex)
            except re.error as error:
                raise ValueError(f"{case.id}: invalid answer_regex: {error}") from error
        if case.expected.hidden_test:
            hidden_test = _safe_child(SUITES_ROOT, case.expected.hidden_test)
            if not hidden_test.is_file():
                raise ValueError(f"{case.id}: hidden test not found: {hidden_test}")
        if case.expected.control_overlay:
            overlay = _safe_child(SUITES_ROOT, case.expected.control_overlay)
            if not overlay.is_dir():
                raise ValueError(f"{case.id}: control overlay not found: {overlay}")
        cases.append(case)
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("coding E2E case ids must be unique")
    return cases


def _assert_no_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"fixture may not contain symlinks: {path}")


def _git(workspace: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and process.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {process.stderr.strip() or process.stdout.strip()}"
        )
    return process.stdout.strip()


def create_workspace(case: CodingCase, keep: bool = False) -> tuple[Path, str]:
    fixture = _safe_child(FIXTURES_ROOT, case.fixture)
    _assert_no_symlinks(fixture)
    if keep:
        workspace = Path(tempfile.mkdtemp(prefix=f"coding-e2e-{case.id}-"))
    else:
        workspace = Path(tempfile.mkdtemp(prefix=f"coding-e2e-{case.id}-"))
    shutil.copytree(fixture, workspace, dirs_exist_ok=True)
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Coding E2E Control")
    _git(workspace, "config", "user.email", "coding-e2e@example.test")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-qm", "fixture baseline")
    return workspace, _git(workspace, "rev-parse", "HEAD")


def remove_workspace(workspace: Path, keep: bool) -> None:
    if not keep:
        shutil.rmtree(workspace, ignore_errors=True)


def _resolve_command(command: tuple[str, ...]) -> tuple[str, ...]:
    if not command:
        raise ValueError("test command must not be empty")
    executable = sys.executable if command[0] == "python" else command[0]
    return (executable, *command[1:])


def run_command(
    workspace: Path,
    command: tuple[str, ...],
    timeout: float = 30,
) -> CommandResult:
    resolved = _resolve_command(command)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(workspace)
    started = time.monotonic()
    try:
        process = subprocess.run(
            list(resolved),
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        returncode = process.returncode
        output = (process.stdout + process.stderr)[-8_000:]
    except subprocess.TimeoutExpired as error:
        returncode = 124
        output = f"timeout after {timeout}s: {error}"
    return CommandResult(
        command=resolved,
        returncode=returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        output=output,
    )


def _fixture_text(workspace: Path) -> str:
    parts: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
    return "\n".join(parts)


def _install_hidden_test(case: CodingCase, workspace: Path) -> None:
    if not case.expected.hidden_test:
        return
    source = _safe_child(SUITES_ROOT, case.expected.hidden_test)
    target_dir = workspace / ".e2e_hidden"
    target_dir.mkdir(exist_ok=True)
    shutil.copy2(source, target_dir / source.name)


def _apply_control_overlay(case: CodingCase, workspace: Path) -> None:
    if not case.expected.control_overlay:
        return
    overlay = _safe_child(SUITES_ROOT, case.expected.control_overlay)
    _assert_no_symlinks(overlay)
    shutil.copytree(overlay, workspace, dirs_exist_ok=True)


def preflight_case(case: CodingCase) -> PreflightResult:
    workspace, _ = create_workspace(case)
    try:
        issues: list[str] = []
        public = run_command(workspace, case.public_test)
        if not public.passed:
            issues.append("fixture public test baseline failed")

        fixture_text = _fixture_text(workspace)
        for term in case.expected.fixture_must_contain:
            if term not in fixture_text:
                issues.append(f"fixture oracle term not found: {term!r}")

        baseline_hidden = None
        control_public = None
        control_hidden = None
        if case.kind == "mutation":
            if not case.expected.hidden_test or not case.expected.control_overlay:
                issues.append("mutation case requires hidden_test and control_overlay")
            else:
                _install_hidden_test(case, workspace)
                hidden_command = (
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    ".e2e_hidden",
                    "-v",
                )
                baseline_hidden = run_command(workspace, hidden_command)
                if baseline_hidden.passed:
                    issues.append("hidden test unexpectedly passes on baseline")
                _apply_control_overlay(case, workspace)
                control_public = run_command(workspace, case.public_test)
                control_hidden = run_command(workspace, hidden_command)
                if not control_public.passed:
                    issues.append("known-good control breaks public tests")
                if not control_hidden.passed:
                    issues.append("known-good control fails hidden tests")

        return PreflightResult(
            case_id=case.id,
            passed=not issues,
            issues=tuple(issues),
            public_test=public,
            baseline_hidden_test=baseline_hidden,
            control_public_test=control_public,
            control_hidden_test=control_hidden,
        )
    finally:
        remove_workspace(workspace, keep=False)


def preflight_suite(cases: list[CodingCase]) -> dict[str, PreflightResult]:
    return {case.id: preflight_case(case) for case in cases}


def capture_changes(workspace: Path, baseline_sha: str) -> tuple[list[str], str]:
    _git(workspace, "add", "-N", "--", ".", check=False)
    names = _git(workspace, "diff", "--name-only", baseline_sha, "--", check=False)
    changed = sorted({line.strip() for line in names.splitlines() if line.strip()})
    diff = _git(workspace, "diff", "--no-ext-diff", baseline_sha, "--", check=False)
    return changed, diff[:64_000]


def _model_call_count(trajectory: Trajectory) -> int:
    return sum(
        1
        for event in trajectory.runtime_events
        if event.get("event_type") == "model_call_completed"
    )


def _run_started_workspace(trajectory: Trajectory) -> str | None:
    for event in trajectory.runtime_events:
        if event.get("event_type") == "run_started":
            return str((event.get("payload") or {}).get("workspace_root") or "")
    return None


def score_live_run(
    case: CodingCase,
    run_index: int,
    preflight: PreflightResult,
    trajectory: Trajectory,
    workspace: Path,
    changed_files: list[str],
    diff: str,
    public_test: CommandResult,
    hidden_test: CommandResult | None,
) -> CodingRunResult:
    harness_issues = list(preflight.issues)
    runtime_issues: list[str] = []
    oracle_issues: list[str] = []

    if trajectory.http_status != 200:
        runtime_issues.append(f"agent HTTP status {trajectory.http_status}")
    if trajectory.trace_source != "runtime_events":
        runtime_issues.append(f"non-canonical trace source: {trajectory.trace_source}")
    if trajectory.error:
        runtime_issues.append(trajectory.error)
    recorded_workspace = _run_started_workspace(trajectory)
    if recorded_workspace and Path(recorded_workspace).resolve() != workspace.resolve():
        runtime_issues.append(
            f"workspace provenance mismatch: {recorded_workspace} != {workspace}"
        )

    answer_lower = trajectory.final_answer.lower()
    for term in case.expected.answer_contains:
        if term.lower() not in answer_lower:
            oracle_issues.append(f"answer missing {term!r}")
    for term in case.expected.answer_not_contains:
        if term.lower() in answer_lower:
            oracle_issues.append(f"answer contains forbidden term {term!r}")
    if case.expected.answer_regex and not re.search(
        case.expected.answer_regex,
        trajectory.final_answer,
        re.IGNORECASE | re.MULTILINE,
    ):
        oracle_issues.append(f"answer regex did not match: {case.expected.answer_regex}")

    called = trajectory.tool_names
    for tool in case.expected.required_tools:
        if tool not in called:
            oracle_issues.append(f"required tool not called: {tool}")
    if case.expected.required_any_tools and not any(
        tool in called for tool in case.expected.required_any_tools
    ):
        oracle_issues.append(
            "none of the acceptable tools were called: "
            f"{list(case.expected.required_any_tools)}"
        )
    for tool in case.expected.forbidden_tools:
        if tool in called:
            oracle_issues.append(f"forbidden tool called: {tool}")
    if case.expected.max_tool_calls is not None and len(called) > case.expected.max_tool_calls:
        oracle_issues.append(
            f"tool calls {len(called)} > {case.expected.max_tool_calls}"
        )
    model_calls = _model_call_count(trajectory)
    if case.expected.max_model_calls is not None and model_calls > case.expected.max_model_calls:
        oracle_issues.append(
            f"model calls {model_calls} > {case.expected.max_model_calls}"
        )
    if trajectory.latency_ms > case.max_latency_ms:
        oracle_issues.append(
            f"latency {trajectory.latency_ms}ms > {case.max_latency_ms}ms"
        )

    changed = set(changed_files)
    if case.expected.no_changes and changed:
        oracle_issues.append(f"read-only task changed files: {sorted(changed)}")
    missing_changes = set(case.expected.required_changed_files) - changed
    if missing_changes:
        oracle_issues.append(f"required changed files missing: {sorted(missing_changes)}")
    forbidden_changes = set(case.expected.forbidden_changed_files) & changed
    if forbidden_changes:
        oracle_issues.append(f"forbidden files changed: {sorted(forbidden_changes)}")
    protected_test_changes = sorted(
        path for path in changed if path == "tests" or path.startswith("tests/")
    )
    if case.kind == "mutation" and protected_test_changes:
        oracle_issues.append(
            f"Agent modified protected public tests: {protected_test_changes}"
        )
    if not public_test.passed:
        oracle_issues.append("public tests failed after Agent run")
    if case.kind == "mutation" and (hidden_test is None or not hidden_test.passed):
        oracle_issues.append("hidden tests failed after Agent run")

    if harness_issues:
        failure_layer = "harness"
    elif runtime_issues:
        protocol_reasons = (
            "tool_protocol_error",
            "max_steps_exceeded",
            "max_tool_calls_exceeded",
            "consecutive_tool_errors_exceeded",
        )
        failure_layer = (
            "model_runtime_protocol"
            if any(reason in issue for issue in runtime_issues for reason in protocol_reasons)
            else "runtime"
        )
    elif oracle_issues:
        failure_layer = "capability_or_agent_policy"
    else:
        failure_layer = "none"

    return CodingRunResult(
        case_id=case.id,
        run_index=run_index,
        passed=failure_layer == "none",
        failure_layer=failure_layer,
        harness_issues=harness_issues,
        runtime_issues=runtime_issues,
        oracle_issues=oracle_issues,
        trajectory=trajectory,
        changed_files=changed_files,
        diff=diff,
        public_test=public_test,
        hidden_test=hidden_test,
        workspace=str(workspace),
    )


async def _switch_workspace(
    client: httpx.AsyncClient,
    agent_url: str,
    jwt: str,
    workspace: Path,
) -> None:
    response = await client.post(
        f"{agent_url.rstrip('/')}/api/workspace/open",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"path": str(workspace)},
        timeout=10,
    )
    response.raise_for_status()
    selected = Path(response.json()["path"]).resolve()
    if selected != workspace.resolve():
        raise RuntimeError(f"workspace switch mismatch: {selected} != {workspace}")


async def _current_workspace(
    client: httpx.AsyncClient,
    agent_url: str,
    jwt: str,
) -> Path:
    response = await client.get(
        f"{agent_url.rstrip('/')}/api/workspace/current",
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=10,
    )
    response.raise_for_status()
    raw_path = response.json().get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Agent did not report a current workspace")
    workspace = Path(raw_path)
    if not workspace.is_dir():
        raise RuntimeError(f"Agent current workspace does not exist: {workspace}")
    return workspace


async def run_live_case(
    case: CodingCase,
    run_index: int,
    preflight: PreflightResult,
    client: httpx.AsyncClient,
    agent_url: str,
    jwt: str,
    keep_workspace: bool,
) -> CodingRunResult:
    workspace, baseline_sha = create_workspace(case, keep=keep_workspace)
    previous_workspace: Path | None = None
    switched = False
    result: CodingRunResult | None = None
    try:
        previous_workspace = await _current_workspace(client, agent_url, jwt)
        await _switch_workspace(client, agent_url, jwt, workspace)
        switched = True
        generic_case = Case(
            id=case.id,
            description=f"coding E2E: {case.kind}",
            prompt=case.prompt,
            model=case.model,
            session_id_pattern="coding-e2e:{id}:{ts}:" + uuid.uuid4().hex[:8],
        )
        trajectory = await _run_one(
            client,
            agent_url,
            jwt,
            generic_case,
            run_idx=run_index,
        )
        changed_files, diff = capture_changes(workspace, baseline_sha)
        public_test = run_command(workspace, case.public_test)
        hidden_test = None
        if case.expected.hidden_test:
            _install_hidden_test(case, workspace)
            hidden_test = run_command(
                workspace,
                (
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    ".e2e_hidden",
                    "-v",
                ),
            )
        result = score_live_run(
            case,
            run_index,
            preflight,
            trajectory,
            workspace,
            changed_files,
            diff,
            public_test,
            hidden_test,
        )
    except Exception as error:  # noqa: BLE001 - live E2E boundary returns attribution
        result = CodingRunResult(
            case_id=case.id,
            run_index=run_index,
            passed=False,
            failure_layer="runtime" if preflight.passed else "harness",
            harness_issues=list(preflight.issues),
            runtime_issues=[f"{type(error).__name__}: {error}"],
            workspace=str(workspace),
        )
    finally:
        restore_failed = False
        if switched and previous_workspace is not None:
            try:
                await _switch_workspace(client, agent_url, jwt, previous_workspace)
            except Exception as error:  # noqa: BLE001 - cleanup is best effort
                restore_failed = True
                log.warning("failed to restore Agent workspace to %s: %s", previous_workspace, error)
                if result is not None:
                    result.passed = False
                    result.failure_layer = "runtime"
                    result.runtime_issues.append(
                        f"workspace restore failed; workspace preserved at {workspace}: {error}"
                    )
        remove_workspace(workspace, keep=keep_workspace or restore_failed)
    if result is None:
        raise RuntimeError("coding E2E run ended without a result")
    return result


async def run_live_suite(
    cases: list[CodingCase],
    preflight: dict[str, PreflightResult],
    *,
    agent_url: str,
    jwt: str,
    runs: int,
    keep_workspaces: bool = False,
) -> list[CodingRunResult]:
    results: list[CodingRunResult] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for case in cases:
            for run_index in range(runs):
                results.append(
                    await run_live_case(
                        case,
                        run_index,
                        preflight[case.id],
                        client,
                        agent_url,
                        jwt,
                        keep_workspaces,
                    )
                )
    return results


def build_report(
    cases: list[CodingCase],
    preflight: dict[str, PreflightResult],
    runs: list[CodingRunResult],
) -> dict[str, Any]:
    by_layer: dict[str, int] = {}
    for run in runs:
        by_layer[run.failure_layer] = by_layer.get(run.failure_layer, 0) + 1
    return {
        "suite": "coding-e2e",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "case_count": len(cases),
        "run_count": len(runs),
        "passed": sum(1 for run in runs if run.passed),
        "failed": sum(1 for run in runs if not run.passed),
        "by_failure_layer": by_layer,
        "preflight": {case_id: asdict(result) for case_id, result in preflight.items()},
        "runs": [run.to_dict() for run in runs],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Coding Agent E2E Report",
        "",
        f"- Cases: {report['case_count']}",
        f"- Live runs: {report['run_count']}",
        f"- Passed: {report['passed']}",
        f"- Failed: {report['failed']}",
        f"- Failure layers: `{json.dumps(report['by_failure_layer'], sort_keys=True)}`",
        "",
        "## Attribution",
        "",
        "- `harness`: fixture, test command, hidden oracle, or control is invalid.",
        "- `runtime`: transport, workspace provenance, event capture, or service failure.",
        "- `model_runtime_protocol`: bounded Agent/model tool protocol failure.",
        "- `capability_or_agent_policy`: runtime succeeded but task postconditions failed.",
        "- `none`: all deterministic postconditions passed.",
        "",
        "## Runs",
        "",
        "| Case | Run | Passed | Layer | Latency | Tools | Changed files |",
        "|---|---:|:---:|---|---:|---|---|",
    ]
    for run in report["runs"]:
        lines.append(
            "| {case} | {index} | {passed} | {layer} | {latency} | {tools} | {files} |".format(
                case=run["case_id"],
                index=run["run_index"],
                passed="yes" if run["passed"] else "no",
                layer=run["failure_layer"],
                latency=run.get("latency_ms") or 0,
                tools=", ".join(run.get("tool_calls") or ()) or "-",
                files=", ".join(run.get("changed_files") or ()) or "-",
            )
        )
        issues = (
            run.get("harness_issues", [])
            + run.get("runtime_issues", [])
            + run.get("oracle_issues", [])
        )
        if issues:
            lines.append(f"\nIssues for `{run['case_id']}` run {run['run_index']}: " + "; ".join(issues))
    return "\n".join(lines) + "\n"


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    _write_private_text(
        output / "report.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
    _write_private_text(output / "report.md", render_markdown(report))


def cli() -> int:
    parser = argparse.ArgumentParser(description="Run fixture-backed coding Agent E2E cases.")
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--agent-url", default=DEFAULT_AGENT_URL)
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL)
    parser.add_argument("--jwt-file", default=DEFAULT_JWT_FILE)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    cases = load_suite(args.suite)
    if args.smoke:
        cases = [case for case in cases if "smoke" in case.tags]
    if args.tag:
        cases = [case for case in cases if any(tag in case.tags for tag in args.tag)]
    if args.case:
        cases = [case for case in cases if case.id in args.case]
    if not cases:
        print("no coding E2E cases matched", file=sys.stderr)
        return 2

    preflight = preflight_suite(cases)
    for case in cases:
        result = preflight[case.id]
        marker = "PASS" if result.passed else "FAIL"
        print(f"[{marker}] preflight {case.id}")
        for issue in result.issues:
            print(f"  - {issue}")

    live_runs: list[CodingRunResult] = []
    if not args.preflight_only and all(result.passed for result in preflight.values()):
        jwt = _ensure_jwt(args.jwt_file, args.auth_url)
        live_runs = asyncio.run(
            run_live_suite(
                cases,
                preflight,
                agent_url=args.agent_url,
                jwt=jwt,
                runs=max(1, args.runs),
                keep_workspaces=args.keep_workspaces,
            )
        )

    report = build_report(cases, preflight, live_runs)
    output = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_ROOT / time.strftime("%Y-%m-%dT%H-%M-%S")
    )
    write_report(output, report)
    print(f"report: {output}")

    preflight_passed = all(result.passed for result in preflight.values())
    live_passed = args.preflight_only or all(run.passed for run in live_runs)
    return 0 if preflight_passed and live_passed else 1


if __name__ == "__main__":
    raise SystemExit(cli())