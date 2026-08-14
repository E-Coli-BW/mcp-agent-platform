#!/usr/bin/env python3
"""SIGKILL a server during retry backoff, then resume it in a new process."""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]


class ScriptedProvider(ThreadingHTTPServer):
    calls: int = 0


class ProviderHandler(BaseHTTPRequestHandler):
    server: ScriptedProvider

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": []})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        self.server.calls += 1
        if self.server.calls == 1:
            self._json(
                500,
                {"error": {"message": "injected crash-retry failure", "type": "server_error"}},
            )
            return
        self._json(
            200,
            {
                "id": f"chatcmpl-{self.server.calls}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "retry-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "RECOVERED"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def auth_headers() -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "recovery-test", "tenant_id": "tenant-a", "iat": now, "exp": now + 300},
        "default-dev-secret-DO-NOT-USE-IN-PRODUCTION",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def wait_for_health(port: int, process: subprocess.Popen, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"agent server exited early:\n{stdout}\n{stderr}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise TimeoutError("agent server did not become healthy")


def start_agent(port: int, provider_port: int, database: Path, config_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "AGENT_PORT": str(port),
            "AGENT_DEFAULT_MODEL": "mlx/retry-test",
            "AGENT_STRONG_MODEL": "mlx/retry-test",
            "AGENT_CHEAP_MODEL": "mlx/retry-test",
            "AGENT_MLX_BASE_URL": f"http://127.0.0.1:{provider_port}",
            "AGENT_AGENT_CONFIG_DIR": str(config_dir),
            "AGENT_PLUGINS_DIR": str(config_dir / "plugins"),
            "AGENT_RUNTIME_STORE_BACKEND": "sqlite",
            "AGENT_RUNTIME_STORE_SQLITE_PATH": str(database),
            "AGENT_MODEL_RETRY_MAX_RETRIES": "2",
            "AGENT_MODEL_RETRY_INITIAL_DELAY_MS": "30000",
            "AGENT_MODEL_RETRY_MAX_DELAY_MS": "30000",
            "AGENT_MODEL_RETRY_JITTER_RATIO": "0",
            "AGENT_LLM_TIMEOUT": "5",
            "AGENT_REDIS_URL": "redis://127.0.0.1:1/0",
            "AGENT_KAFKA_BOOTSTRAP_SERVERS": "",
            "AGENT_SKILLS_INDEX_ENABLED": "false",
            "AGENT_REFLEXION_ENABLED": "false",
            "AGENT_DIRECT_TOOL_ROUTING_ENABLED": "false",
            "AGENT_SUBAGENT_VERIFIER_ENABLED": "false",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def wait_for_checkpoint(database: Path, timeout: float = 15) -> tuple[str, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    """SELECT run_id, payload_json FROM agent_run_checkpoints
                    WHERE state = 'model_retry_waiting' LIMIT 1"""
                ).fetchone()
            if row is not None:
                return str(row[0]), json.loads(row[1])
        except sqlite3.OperationalError:
            pass
        time.sleep(0.05)
    raise TimeoutError("model retry checkpoint did not reach waiting state")


def stop_process(process: subprocess.Popen, sig: signal.Signals) -> None:
    if process.poll() is None:
        os.killpg(process.pid, sig)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="model-retry-crash-") as directory:
        root = Path(directory)
        database = root / "runtime.sqlite3"
        config_dir = root / "empty-config"
        workspace = root / "workspace"
        config_dir.mkdir()
        workspace.mkdir()

        provider = ScriptedProvider(("127.0.0.1", 0), ProviderHandler)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        provider_port = int(provider.server_address[1])
        first_port = free_port()
        first = start_agent(first_port, provider_port, database, config_dir)
        requester_error: list[str] = []
        second: subprocess.Popen | None = None
        try:
            wait_for_health(first_port, first)
            httpx.post(
                f"http://127.0.0.1:{first_port}/api/workspace/open",
                json={"path": str(workspace)},
                timeout=5,
            ).raise_for_status()

            def send_chat() -> None:
                try:
                    httpx.post(
                        f"http://127.0.0.1:{first_port}/v1/chat/completions",
                        headers=auth_headers(),
                        json={
                            "model": "mlx/retry-test",
                            "messages": [{"role": "user", "content": "recover after crash"}],
                            "session_id": "crash-retry",
                            "temperature": 0,
                            "stream": False,
                        },
                        timeout=40,
                    )
                except Exception as error:  # noqa: BLE001 - SIGKILL must break this request
                    requester_error.append(type(error).__name__)

            requester = threading.Thread(target=send_chat)
            requester.start()
            run_id, payload = wait_for_checkpoint(database)
            stop_process(first, signal.SIGKILL)
            requester.join(timeout=5)

            with sqlite3.connect(database) as connection:
                connection.execute(
                    """UPDATE agent_run_checkpoints
                    SET payload_json = json_set(payload_json, '$.resume_at', ?),
                        lease_expires_at = ?
                    WHERE run_id = ?""",
                    (time.time() - 1, time.time() - 1, run_id),
                )

            second_port = free_port()
            second = start_agent(second_port, provider_port, database, config_dir)
            wait_for_health(second_port, second)
            recoverable = httpx.get(
                f"http://127.0.0.1:{second_port}/v1/runtime/recoverable",
                headers=auth_headers(),
                timeout=5,
            )
            recoverable.raise_for_status()
            resumed = httpx.post(
                f"http://127.0.0.1:{second_port}/v1/runs/{run_id}/resume",
                headers=auth_headers(),
                timeout=20,
            )
            resumed.raise_for_status()
            body = resumed.json()
            events = httpx.get(
                f"http://127.0.0.1:{second_port}/v1/runs/{run_id}/events",
                headers=auth_headers(),
                timeout=5,
            ).json()
            if body.get("accepted") is not True or body.get("response") != "RECOVERED":
                raise AssertionError(f"resume failed: {body}")
            if body["checkpoint"]["state"] != "completed":
                raise AssertionError(f"checkpoint not completed: {body}")
            if provider.calls != 2:
                raise AssertionError(f"provider call count mismatch: {provider.calls}")
            if events.get("status") != "completed":
                raise AssertionError(f"run did not complete: {events}")
            if events.get("terminal_reason") != "model_retry_recovered":
                raise AssertionError(f"wrong terminal reason: {events}")
            output = {
                "passed": True,
                "run_id": run_id,
                "attempts_failed_before_crash": payload["attempts_failed"],
                "provider_calls": provider.calls,
                "requester_error": requester_error,
                "recoverable_count": len(recoverable.json()["checkpoints"]),
                "checkpoint_state": body["checkpoint"]["state"],
                "response": body["response"],
                "run_status": events["status"],
                "terminal_reason": events["terminal_reason"],
            }
            print(json.dumps(output))
        finally:
            stop_process(first, signal.SIGKILL)
            if second is not None:
                stop_process(second, signal.SIGTERM)
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()