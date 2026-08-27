"""Reproduce paired cold/warm local-agent trials without manual selection."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HARNESS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = HARNESS_ROOT.parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

IMPLEMENTATION_FILES = (
    "platform/agent-server/agents/coding-agent.yaml",
    "platform/agent-server/app/agent/agent_graph/factory.py",
    "platform/agent-server/app/agent/agent_graph/builder.py",
    "platform/agent-server/app/agent/agent_graph/nodes/context.py",
    "platform/agent-server/app/agent/agent_graph/nodes/intent_router.py",
    "platform/agent-server/app/agent/agent_graph/nodes/model.py",
    "platform/agent-server/app/agent/engine/registry.py",
    "platform/agent-server/app/agent/engine/langgraph.py",
    "platform/agent-server/app/agent/model/factory.py",
    "platform/agent-server/app/api/chat.py",
    "platform/agent-server/app/runtime/chat_stream.py",
    "platform/agent-server/app/runtime/runner.py",
    "platform/agent-server/app/runtime/composition.py",
    "platform/agent-server/app/runtime/observers/default.py",
    "platform/agent-server/app/runtime/observers/provenance.py",
    "platform/agent-server/app/events/model_provenance.py",
    "platform/eval-harness/eval/warm_cache.py",
    "platform/eval-harness/experiments/local_runtime_20260826/run.py",
    "platform/agent-server/tests/test_local_runtime_effect.py",
    "platform/agent-server/tests/test_runtime_observers.py",
)

from eval.runner import _ensure_jwt
from eval.warm_cache import run_benchmark


PROMPTS = [
    "read README.md",
    "Using the README content from the previous turn, name the main runtime components.",
]


def _require_private_file(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"JWT file must be owner-only (chmod 600): {path} has mode {mode:o}"
        )


def _git_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
    ).strip()


def _implementation_hashes() -> dict[str, str]:
    return {
        relative_path: hashlib.sha256(
            (REPO_ROOT / relative_path).read_bytes()
        ).hexdigest()
        for relative_path in IMPLEMENTATION_FILES
    }


def _ollama_metadata(client: httpx.Client, base_url: str, model: str) -> dict[str, Any]:
    tags = client.get(f"{base_url}/api/tags").json().get("models", [])
    tag = next(item for item in tags if item.get("name") == model)
    show = client.post(f"{base_url}/api/show", json={"model": model}).json()
    return {
        "model": model,
        "digest": tag.get("digest"),
        "size_bytes": tag.get("size"),
        "modified_at": tag.get("modified_at"),
        "details": tag.get("details"),
        "template_sha256": hashlib.sha256(
            str(show.get("template", "")).encode("utf-8")
        ).hexdigest(),
        "context_lengths": {
            key: value
            for key, value in show.get("model_info", {}).items()
            if "context_length" in key
        },
    }


def _unload_model(client: httpx.Client, base_url: str, model: str) -> None:
    response = client.post(
        f"{base_url}/api/generate",
        json={"model": model, "keep_alive": 0, "stream": False},
    )
    response.raise_for_status()


def _validate_pair(result: dict[str, Any]) -> list[str]:
    errors = []
    cold, warm = result["turns"]
    if cold.get("run_status") != "completed" or warm.get("run_status") != "completed":
        errors.append("both turns must complete")
    cold_tools = cold.get("tool_calls", [])
    warm_tools = warm.get("tool_calls", [])
    if not any(
        call.get("tool") == "file_read" and call.get("status") == "ok"
        for call in cold_tools
    ):
        errors.append("cold turn must complete file_read successfully")
    if any(call.get("status") == "error" for call in cold_tools + warm_tools):
        errors.append("tool errors are not valid benchmark trials")
    if any(call.get("tool") == "file_read" for call in warm_tools):
        errors.append("warm follow-up must reuse prior file context")
    if not result.get("comparison", {}).get("stable_prefix_match"):
        errors.append("system prefix hash must remain stable")
    return errors


async def _run(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in output_dir.glob("pair-*.json"):
        stale_path.unlink()
    (output_dir / "manifest.json").unlink(missing_ok=True)
    jwt_path = args.jwt_file.expanduser().resolve()
    jwt = _ensure_jwt(str(jwt_path), args.auth_url)
    _require_private_file(jwt_path)

    with httpx.Client(timeout=30) as ollama:
        metadata = _ollama_metadata(ollama, args.ollama_url, args.ollama_model)
        trials = []
        exit_code = 0
        for index in range(1, args.pairs + 1):
            _unload_model(ollama, args.ollama_url, args.ollama_model)
            session_id = f"{args.session_prefix}-{index}-{int(time.time())}"
            result = await run_benchmark(
                prompts=PROMPTS,
                agent_url=args.agent_url,
                jwt=jwt,
                model=args.agent_model,
                session_id=session_id,
            )
            validation_errors = _validate_pair(result)
            result["validation"] = {
                "accepted": not validation_errors,
                "errors": validation_errors,
            }
            trial_path = output_dir / f"pair-{index}.json"
            trial_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            trials.append(
                {
                    "pair": index,
                    "file": trial_path.name,
                    "accepted": not validation_errors,
                    "errors": validation_errors,
                }
            )
            if validation_errors:
                exit_code = 1

    manifest = {
        "protocol": 1,
        "base_commit": _git_commit(),
        "implementation": {
            "note": (
                "The experiment used an uncommitted refactoring over "
                "base_commit; these hashes pin the exact relevant contents."
            ),
            "sha256": _implementation_hashes(),
        },
        "agent_url": args.agent_url,
        "agent_model": args.agent_model,
        "ollama_url": args.ollama_url,
        "ollama": metadata,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "prompts": PROMPTS,
        "cold_reset": "POST /api/generate with keep_alive=0 before every pair",
        "selection": "all generated pairs are retained; validation is automatic",
        "trials": trials,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return exit_code


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="http://127.0.0.1:8580")
    parser.add_argument("--agent-model", default="coding-agent")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ollama-model", default="qwen2.5:7b")
    parser.add_argument("--auth-url", default="http://127.0.0.1:8090")
    parser.add_argument(
        "--jwt-file",
        type=Path,
        default=Path.home() / ".local/state/mcp-agent/eval-jwt.txt",
    )
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--session-prefix", default="local-runtime")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/local_runtime_20260826/results/latest"),
    )
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    cli()