#!/usr/bin/env python3
"""Run API-level runtime scenarios against a local agent server.

This is a manual integration/evaluation harness, not a unit test. It drives the
HTTP API like a real client, collects SSE output, then queries the runtime event
endpoint to validate the contract introduced by the runtime refactor.

Usage:
    cd platform/agent-server
    .venv/bin/python scripts/runtime_scenario.py \
      --base-url http://localhost:8500 \
      --scenario readme_summary
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


DEFAULT_DEV_SECRET = "default-dev-secret-DO-NOT-USE-IN-PRODUCTION"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_hmac_token(*, tenant_id: str, secret: str, sub: str = "scenario-user") -> str:
    secret_bytes = secret.encode()
    if len(secret_bytes) < 32:
        secret_bytes = secret_bytes.ljust(32, b"\x00")

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({
            "sub": sub,
            "tenant_id": tenant_id,
            "exp": int(time.time()) + 1800,
        }).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(hmac.new(secret_bytes, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


@dataclass
class StreamResult:
    content: str = ""
    run_id: str = ""
    status_events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)


def _parse_sse_response(response: httpx.Response) -> StreamResult:
    result = StreamResult()
    current: dict[str, str] = {}

    def flush() -> None:
        if not current:
            return
        event_name = current.get("event", "message")
        data = current.get("data", "")
        event = {
            "id": current.get("id"),
            "event": event_name,
            "data": data,
        }
        result.raw_events.append(event)

        if event_name == "status":
            payload = json.loads(data)
            result.status_events.append(payload)
            result.run_id = payload.get("run_id") or result.run_id
        elif event_name.startswith("tool_"):
            result.tool_events.append(json.loads(data))
        elif data and data != "[DONE]":
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                return
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            result.content += delta.get("content", "")

    for line in response.iter_lines():
        if not line:
            flush()
            current = {}
            continue
        if line.startswith(":"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key] = value.lstrip()
    flush()
    return result


def prepare_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text(
        "# Runtime Scenario\n\n"
        "This workspace exists to test agent runtime events and tool usage.\n",
        encoding="utf-8",
    )


def open_workspace(client: httpx.Client, base_url: str, workspace: Path) -> None:
    response = client.post(
        f"{base_url}/api/workspace/open",
        json={"path": str(workspace)},
    )
    response.raise_for_status()


def run_chat(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    session_id: str,
    prompt: str,
) -> StreamResult:
    response = client.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "coding-agent",
            "session_id": session_id,
            "stream": True,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    response.raise_for_status()
    return _parse_sse_response(response)


def run_cancel_chat(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    session_id: str,
) -> tuple[StreamResult, dict[str, Any]]:
    """Cancel a live SSE request after its run id becomes visible."""
    result = StreamResult()
    current: dict[str, str] = {}
    cancel_response: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cancel_response
        if not current:
            return
        event_name = current.get("event", "message")
        data = current.get("data", "")
        event = {"id": current.get("id"), "event": event_name, "data": data}
        result.raw_events.append(event)
        if event_name == "status":
            payload = json.loads(data)
            result.status_events.append(payload)
            result.run_id = payload.get("run_id") or result.run_id
            if result.run_id and cancel_response is None:
                cancel_response = client.post(
                    f"{base_url}/v1/runs/{result.run_id}/cancel",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                cancel_response.raise_for_status()
        elif data and data != "[DONE]":
            try:
                chunk = json.loads(data)
                result.content += chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            except json.JSONDecodeError:
                pass

    with client.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "coding-agent",
            "session_id": session_id,
            "stream": True,
            "temperature": 0,
            "messages": [{"role": "user", "content": "Explain this workspace in detail."}],
        },
        timeout=180,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                flush()
                current = {}
            elif not line.startswith(":") and ":" in line:
                key, value = line.split(":", 1)
                current[key] = value.lstrip()
        flush()

    if not cancel_response:
        raise AssertionError("stream never exposed a run_id for cancellation")
    return result, cancel_response.json()


def fetch_runtime_events(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    run_id: str,
) -> dict[str, Any]:
    response = client.get(
        f"{base_url}/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def assert_restart_recovery(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    run_id: str,
    expected_event_ids: list[str],
) -> None:
    """Validate the same SQLite-backed run through a separately started API."""
    runtime = fetch_runtime_events(
        client, base_url=base_url, token=token, run_id=run_id
    )
    actual_event_ids = [event["event_id"] for event in runtime["events"]]
    if actual_event_ids != expected_event_ids:
        raise AssertionError(
            "restart recovery changed the event timeline: "
            f"expected={expected_event_ids}, actual={actual_event_ids}"
        )
    print(f"   restart_recovery=ok via {base_url}")


def assert_durable_file_write(
    *,
    database_path: str,
    run_id: str,
) -> None:
    """Verify duplicate tool requests collapse to one durable invocation."""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """SELECT COUNT(*), MIN(status), MAX(tool_name)
               FROM agent_tool_invocations
               WHERE run_id = ? AND tool_name = 'file_write'""",
            (run_id,),
        ).fetchone()
    count, status, tool_name = row
    if count != 1 or status != "success" or tool_name != "file_write":
        raise AssertionError(
            "durable file_write idempotency failed: "
            f"count={count}, status={status!r}, tool={tool_name!r}"
        )
    print("   durable_file_write=ok (one successful invocation)")


def assert_runtime_contract(stream: StreamResult, runtime: dict[str, Any]) -> None:
    if not stream.run_id:
        raise AssertionError("stream did not include a run_id status event")
    if runtime["run_id"] != stream.run_id:
        raise AssertionError("runtime run_id does not match stream run_id")
    if runtime["status"] not in {"completed", "cancelled"}:
        raise AssertionError(f"unexpected run status: {runtime['status']}")
    if runtime["status"] == "completed":
        if runtime.get("terminal_reason") != "completed":
            raise AssertionError("completed run missing terminal_reason=completed")
        if runtime.get("retryable") is not False:
            raise AssertionError("completed run must not be retryable")

    event_types = [event["event_type"] for event in runtime["events"]]
    if "run_started" not in event_types:
        raise AssertionError("runtime events missing run_started")
    if not any(t in event_types for t in ("run_completed", "run_cancelled")):
        raise AssertionError("runtime events missing terminal event")

    tool_started = [
        event for event in runtime["events"]
        if event["event_type"] == "tool_started"
    ]
    for event in tool_started:
        payload = event["payload"]
        if "invocation" not in payload:
            raise AssertionError("tool_started missing invocation envelope")
        if "policy_decision" not in payload:
            raise AssertionError("tool_started missing policy_decision")


def scenario_readme_summary(client: httpx.Client, args: argparse.Namespace, token: str) -> None:
    prompt = "Read the README.md in this workspace and summarize it in one sentence."
    stream = run_chat(
        client,
        base_url=args.base_url,
        token=token,
        session_id=args.session_id,
        prompt=prompt,
    )
    runtime = fetch_runtime_events(
        client,
        base_url=args.base_url,
        token=token,
        run_id=stream.run_id,
    )
    assert_runtime_contract(stream, runtime)
    if "Runtime Scenario" not in stream.content and "runtime" not in stream.content.lower():
        raise AssertionError("final answer does not appear grounded in README.md")
    if args.restarted_base_url:
        assert_restart_recovery(
            client,
            base_url=args.restarted_base_url,
            token=token,
            run_id=stream.run_id,
            expected_event_ids=[event["event_id"] for event in runtime["events"]],
        )
    print_report("readme_summary", stream, runtime)


def scenario_file_write(client: httpx.Client, args: argparse.Namespace, token: str) -> None:
    prompt = "Create hello.py that prints hello, then briefly say what you changed."
    stream = run_chat(
        client,
        base_url=args.base_url,
        token=token,
        session_id=args.session_id,
        prompt=prompt,
    )
    runtime = fetch_runtime_events(
        client,
        base_url=args.base_url,
        token=token,
        run_id=stream.run_id,
    )
    assert_runtime_contract(stream, runtime)
    hello = Path(args.workspace) / "hello.py"
    if not hello.exists():
        raise AssertionError("expected hello.py to be created")
    tool_events = [
        event for event in runtime["events"]
        if event["event_type"] == "tool_started"
    ]
    if not any(event["payload"].get("tool") == "file_write" for event in tool_events):
        raise AssertionError("runtime events missing file_write")
    if args.runtime_db:
        assert_durable_file_write(
            database_path=args.runtime_db,
            run_id=stream.run_id,
        )
    if args.restarted_base_url:
        assert_restart_recovery(
            client,
            base_url=args.restarted_base_url,
            token=token,
            run_id=stream.run_id,
            expected_event_ids=[event["event_id"] for event in runtime["events"]],
        )
    print_report("file_write", stream, runtime)


def scenario_cancel_run(client: httpx.Client, args: argparse.Namespace, token: str) -> None:
    stream, cancel_response = run_cancel_chat(
        client,
        base_url=args.base_url,
        token=token,
        session_id=args.session_id,
    )
    if cancel_response.get("accepted") is not True:
        raise AssertionError(f"cancel request was not accepted: {cancel_response}")
    runtime = fetch_runtime_events(
        client, base_url=args.base_url, token=token, run_id=stream.run_id
    )
    if runtime["status"] != "cancelled":
        raise AssertionError(f"expected cancelled run, got {runtime['status']}")
    if runtime.get("terminal_reason") != "cancel_requested":
        raise AssertionError("cancelled run missing terminal_reason=cancel_requested")
    if runtime.get("retryable") is not True:
        raise AssertionError("cancelled run must be retryable")
    if not any(event["event_type"] == "run_cancelled" for event in runtime["events"]):
        raise AssertionError("runtime events missing run_cancelled")
    if not any(event.get("data", "").find('"state": "cancelled"') >= 0 for event in stream.raw_events):
        raise AssertionError("SSE stream missing cancelled status")
    print_report("cancel_run", stream, runtime)


def scenario_failed_tool(client: httpx.Client, args: argparse.Namespace, token: str) -> None:
    """Verify a tool-level failure is observable without corrupting the run."""
    stream = run_chat(
        client,
        base_url=args.base_url,
        token=token,
        session_id=args.session_id,
        prompt="Use file_read to read this exact missing path: missing-runtime-scenario-file.txt",
    )
    runtime = fetch_runtime_events(
        client, base_url=args.base_url, token=token, run_id=stream.run_id
    )
    assert_runtime_contract(stream, runtime)
    failed_tools = [
        event for event in runtime["events"]
        if event["event_type"] == "tool_completed"
        and str(event["payload"].get("output_preview", "")).startswith("❌")
    ]
    if not failed_tools:
        raise AssertionError("expected an observable file_read tool failure")
    if runtime["status"] != "completed":
        raise AssertionError("tool-level failure should not silently change run status")
    print_report("failed_tool", stream, runtime)


def print_report(name: str, stream: StreamResult, runtime: dict[str, Any]) -> None:
    tool_names = [
        event["payload"].get("tool")
        for event in runtime["events"]
        if event["event_type"] == "tool_started"
    ]
    print(f"✅ scenario={name}")
    print(f"   run_id={stream.run_id}")
    print(f"   status={runtime['status']}")
    print(f"   tools={tool_names}")
    print(f"   answer_preview={stream.content[:200].strip()!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8500")
    parser.add_argument(
        "--restarted-base-url",
        default="",
        help="second server process using the same SQLite runtime DB",
    )
    parser.add_argument(
        "--runtime-db",
        default="",
        help="SQLite runtime DB used to assert durable file_write idempotency",
    )
    parser.add_argument("--tenant-id", default="scenario-tenant")
    parser.add_argument("--session-id", default=f"scenario-{int(time.time())}")
    parser.add_argument("--jwt-secret", default=DEFAULT_DEV_SECRET)
    parser.add_argument("--workspace", default="")
    parser.add_argument(
        "--scenario",
        choices=["readme_summary", "file_write", "cancel_run", "failed_tool"],
        default="readme_summary",
    )
    args = parser.parse_args()

    if not args.workspace:
        args.workspace = tempfile.mkdtemp(prefix="mcp-runtime-scenario-")
    workspace = Path(args.workspace).resolve()
    prepare_workspace(workspace)

    token = mint_hmac_token(tenant_id=args.tenant_id, secret=args.jwt_secret)
    with httpx.Client() as client:
        open_workspace(client, args.base_url, workspace)
        if args.scenario == "readme_summary":
            scenario_readme_summary(client, args, token)
        elif args.scenario == "file_write":
            scenario_file_write(client, args, token)
        elif args.scenario == "cancel_run":
            scenario_cancel_run(client, args, token)
        elif args.scenario == "failed_tool":
            scenario_failed_tool(client, args, token)


if __name__ == "__main__":
    main()
