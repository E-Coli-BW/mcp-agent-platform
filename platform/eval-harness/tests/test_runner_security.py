from __future__ import annotations

import json
import stat
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from eval.runner import (
    _eval_identity,
    _cached_token_matches_auth,
    _runtime_event_issues,
    _runtime_failure,
    _tool_calls_from_events,
    _usage_from_events,
    _write_private_text,
)


def test_write_private_text_uses_owner_only_permissions(tmp_path):
    target = tmp_path / "state" / "token.txt"

    _write_private_text(target, "secret-token")

    assert target.read_text() == "secret-token"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_eval_identity_is_stable_and_token_file_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_USERNAME", raising=False)
    monkeypatch.delenv("EVAL_PASSWORD", raising=False)

    first = _eval_identity(str(tmp_path / "first.jwt"))
    repeated = _eval_identity(str(tmp_path / "first.jwt"))
    second = _eval_identity(str(tmp_path / "second.jwt"))

    assert first == repeated
    assert first[0].startswith("eval-")
    assert first[0] != second[0]
    assert len(first[1]) >= 8


def test_cached_token_requires_valid_signature_and_current_kid(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "current-key"
    token = jwt.encode(
        {
            "sub": "eval-user",
            "aud": "agent-server",
            "iss": "mcp-auth-service",
            "exp": int(time.time()) + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "current-key"},
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [public_jwk]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            return Response()

    monkeypatch.setattr("eval.runner.httpx.Client", Client)

    assert _cached_token_matches_auth(token, "http://auth")
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            "sub": "eval-user",
            "aud": "agent-server",
            "iss": "mcp-auth-service",
            "exp": int(time.time()) + 300,
        },
        forged_key,
        algorithm="RS256",
        headers={"kid": "current-key"},
    )
    assert not _cached_token_matches_auth(forged, "http://auth")


def test_runtime_events_are_canonical_tool_and_usage_source():
    events = [
        {
            "event_seq": 1,
            "event_type": "run_started",
            "timestamp": 9.9,
            "payload": {},
        },
        {
            "event_seq": 2,
            "event_type": "tool_started",
            "timestamp": 10.0,
            "payload": {"seq": 1, "tool": "file_read", "input": {"path": "README.md"}},
        },
        {
            "event_seq": 3,
            "event_type": "tool_completed",
            "timestamp": 10.125,
            "payload": {
                "seq": 1,
                "tool": "file_read",
                "output_preview": "File: README.md",
                "status": "ok",
            },
        },
        {
            "event_seq": 4,
            "event_type": "model_call_completed",
            "payload": {"prompt_tokens": 100, "completion_tokens": 8},
        },
        {
            "event_seq": 5,
            "event_type": "model_call_completed",
            "payload": {"prompt_tokens": 120, "completion_tokens": 12},
        },
        {
            "event_seq": 6,
            "event_type": "run_completed",
            "payload": {},
        },
    ]

    calls = _tool_calls_from_events(events)

    assert len(calls) == 1
    assert calls[0].tool == "file_read"
    assert calls[0].input == {"path": "README.md"}
    assert calls[0].output == "File: README.md"
    assert calls[0].duration_ms == 125
    assert _usage_from_events(events) == (220, 20)
    assert _runtime_event_issues(events, "completed") == []


def test_runtime_tool_repr_error_is_detected():
    events = [
        {
            "event_type": "tool_started",
            "timestamp": 1.0,
            "payload": {"seq": 1, "tool": "file_read", "input": {}},
        },
        {
            "event_type": "tool_completed",
            "timestamp": 1.1,
            "payload": {
                "seq": 1,
                "tool": "file_read",
                "output_preview": "content='❌ File not found' name='file_read'",
            },
        },
    ]

    assert _tool_calls_from_events(events)[0].status == "error"


def test_runtime_event_validation_rejects_incomplete_tool_pair():
    events = [
        {"event_seq": 1, "event_type": "run_started", "payload": {}},
        {
            "event_seq": 2,
            "event_type": "tool_started",
            "payload": {"seq": 1, "tool": "file_read"},
        },
        {"event_seq": 3, "event_type": "run_completed", "payload": {}},
    ]

    assert _runtime_event_issues(events, "completed") == [
        "tool calls missing completion: [1]"
    ]


def test_runtime_event_validation_rejects_bad_order_status_and_tool_name():
    events = [
        {"event_seq": 2, "event_type": "run_started", "payload": {}},
        {
            "event_seq": 1,
            "event_type": "tool_started",
            "payload": {"seq": 7, "tool": "file_read"},
        },
        {
            "event_seq": 3,
            "event_type": "tool_completed",
            "payload": {"seq": 7, "tool": "file_edit", "status": "unknown"},
        },
        {"event_seq": 4, "event_type": "run_completed", "payload": {}},
    ]

    issues = _runtime_event_issues(events, "completed")

    assert "runtime event_seq is not increasing: 2, 1" in issues
    assert "tool_completed seq 7 has invalid status" in issues
    assert "tool seq 7 name mismatch: file_read != file_edit" in issues


def test_runtime_failure_surfaces_terminal_reason():
    body = {
        "status": "failed",
        "terminal_reason": "tool_protocol_error",
        "events": [
            {
                "event_type": "run_failed",
                "payload": {"error_message": "malformed tool call"},
            }
        ],
    }

    assert _runtime_failure(body) == (
        "runtime failed [tool_protocol_error]: malformed tool call"
    )