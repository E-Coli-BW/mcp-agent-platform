#!/usr/bin/env python3
"""Black-box HTTP/SSE approval recovery acceptance.

Requires auth/memory/codeexec services to be running. The script owns two
agent processes on 8580/8581 and tears them down on exit.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from runtime_scenario import mint_hmac_token


def wait_health(base_url: str) -> None:
    for _ in range(30):
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"agent did not become healthy: {base_url}")


def start_agent(port: int, db: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "AGENT_RUNTIME_STORE_BACKEND": "sqlite",
        "AGENT_RUNTIME_STORE_SQLITE_PATH": str(db),
        "AGENT_TEST_REQUIRE_APPROVAL_TOOLS": "code_shell",
        "AGENT_STRICT_TOOLS": "true",
        "AUTH_SERVER_URL": "http://localhost:8090",
        "MEMORY_SERVER_URL": "http://localhost:8180",
        "CODEEXEC_SERVER_URL": "http://localhost:8380",
        "PYTHONUNBUFFERED": "1",
    })
    return subprocess.Popen([
        str(Path(__file__).resolve().parents[1] / ".venv/bin/uvicorn"),
        "app.main:app", "--host", "127.0.0.1", "--port", str(port),
    ], cwd=Path(__file__).resolve().parents[1], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_until_approval(response: httpx.Response) -> dict:
    current = {}
    for line in response.iter_lines():
        if not line:
            current = {}
            continue
        if line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = line[5:].strip()
            if current.get("event") == "approval_required":
                return json.loads(current["data"])
    raise AssertionError("SSE stream did not expose approval_required")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-db", default="")
    parser.add_argument("--first-port", type=int, default=8581)
    parser.add_argument("--second-port", type=int, default=8582)
    args = parser.parse_args()
    db = Path(args.runtime_db or tempfile.mktemp(prefix="mcp-approval-api-", suffix=".sqlite3"))
    workspace = Path(tempfile.mkdtemp(prefix="mcp-approval-api-workspace-"))
    (workspace / "README.md").write_text("approval recovery workspace\n", encoding="utf-8")
    token = mint_hmac_token(tenant_id="scenario-tenant", secret="default-dev-secret-DO-NOT-USE-IN-PRODUCTION")
    headers = {"Authorization": f"Bearer {token}"}
    first = second = None
    try:
        first = start_agent(args.first_port, db)
        first_base = f"http://localhost:{args.first_port}"
        second_base = f"http://localhost:{args.second_port}"
        wait_health(first_base)
        with httpx.Client() as client:
            client.post(f"{first_base}/api/workspace/open", json={"path": str(workspace)}).raise_for_status()
            with client.stream("POST", f"{first_base}/v1/chat/completions", headers=headers, json={
                "model": "coding-agent", "session_id": "approval-api-recovery", "stream": True,
                "messages": [{"role": "user", "content": "Use code_shell to run printf recovery, then summarize the result."}],
            }, timeout=180) as response:
                response.raise_for_status()
                approval = read_until_approval(response)
            first.terminate(); first.wait(timeout=10); first = None

            second = start_agent(args.second_port, db)
            wait_health(second_base)
            base = second_base
            decision = client.post(f"{base}/v1/approvals/{approval['approval_id']}/decision", headers=headers, json={"status": "approved"})
            decision.raise_for_status()
            resumed = client.post(f"{base}/v1/runs/{approval['run_id']}/resume", headers=headers)
            resumed.raise_for_status()
            checkpoint = client.get(f"{base}/v1/runs/{approval['run_id']}/checkpoint", headers=headers)
            checkpoint.raise_for_status()
            if checkpoint.json()["state"] != "completed":
                raise AssertionError(f"resume did not complete: {checkpoint.json()}")
            followup = client.post(f"{base}/v1/chat/completions", headers=headers, json={
                "model": "coding-agent", "session_id": "approval-api-recovery", "stream": False,
                "messages": [{"role": "user", "content": "Summarize the recovery command result."}],
            }, timeout=180)
            followup.raise_for_status()
            answer = followup.json()["choices"][0]["message"]["content"]
            if "recovery" not in answer.lower():
                raise AssertionError(f"reattached final answer was not grounded in recovery output: {answer!r}")
        print(json.dumps({"passed": True, "approval_id": approval["approval_id"], "run_id": approval["run_id"], "checkpoint": checkpoint.json(), "answer_preview": answer[:200]}))
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
