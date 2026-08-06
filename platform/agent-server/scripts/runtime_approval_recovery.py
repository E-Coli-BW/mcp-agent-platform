#!/usr/bin/env python3
"""Two-process approval/checkpoint recovery acceptance scenario.

This intentionally uses a deterministic continuation handler instead of a
model, so failures represent runtime recovery bugs rather than model variance.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run_phase(script: Path, phase: str, database: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(script), "--phase", phase, "--runtime-db", str(database)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def phase_prepare(database: Path) -> dict:
    from app.runtime.approval import ApprovalRegistry
    from app.runtime.checkpoint import SQLiteCheckpointStore

    approval = __import__("asyncio").run(
        ApprovalRegistry(str(database)).create(
            run_id="run-approval-recovery",
            tenant_id="scenario-tenant",
            tool_name="code_shell",
            reason="deterministic recovery acceptance",
        )
    )
    SQLiteCheckpointStore(database).save(
        run_id=approval.run_id,
        step_seq=1,
        state="waiting_approval",
        tool_name=approval.tool_name,
        approval_id=approval.approval_id,
        payload={"session_id": "scenario-tenant:recovery", "arguments": {"command": "printf recovery"}},
    )
    return {"approval_id": approval.approval_id, "run_id": approval.run_id, "phase": "prepared"}


def phase_resume(database: Path) -> dict:
    import asyncio
    from app.runtime.approval import ApprovalRegistry
    from app.runtime.checkpoint import SQLiteCheckpointStore
    from app.runtime.continuation import ContinuationWorker

    registry = ApprovalRegistry(str(database))
    store = SQLiteCheckpointStore(database)
    recoverable = store.list_recoverable()
    if len(recoverable) != 1 or recoverable[0].state != "waiting_approval":
        raise AssertionError(f"unexpected recoverable checkpoints: {recoverable}")
    checkpoint = recoverable[0]
    asyncio.run(registry.decide(checkpoint.approval_id, status="approved", decided_by="recovery-user"))
    checkpoint = store.transition(checkpoint.run_id, state="approved")
    calls = []
    result = ContinuationWorker(store, "process-b").run_once(
        checkpoint.run_id,
        lambda item: calls.append(item.payload["arguments"]["command"]) or "recovery",
    )
    if result is None or result.state != "completed":
        raise AssertionError(f"recovery did not complete: {result}")
    if calls != ["printf recovery"]:
        raise AssertionError(f"side effect count mismatch: {calls}")
    return {"phase": "resumed", "run_id": result.run_id, "state": result.state, "side_effects": calls}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["prepare", "resume"])
    parser.add_argument("--runtime-db", default="")
    args = parser.parse_args()
    database = Path(args.runtime_db or tempfile.mktemp(prefix="mcp-approval-recovery-", suffix=".sqlite3"))
    if args.phase == "prepare":
        output = phase_prepare(database)
    elif args.phase == "resume":
        output = phase_resume(database)
    else:
        script = Path(__file__).resolve()
        first = run_phase(script, "prepare", database)
        second = run_phase(script, "resume", database)
        output = {"passed": True, "prepare": first, "resume": second, "runtime_db": str(database)}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
