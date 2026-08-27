"""Warm-context benchmark for local coding-agent inference.

Runs sequential prompts in one Agent session, then joins each chat response to
its durable runtime events so prefill/decode timings can be compared directly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from eval.runner import (
    DEFAULT_AGENT_URL,
    DEFAULT_AUTH_URL,
    DEFAULT_JWT_FILE,
    HTTP_TIMEOUT,
    _ensure_jwt,
)


DEFAULT_PROMPTS = [
    "Read README.md and summarize the project's purpose in two sentences.",
    "Using the README context from the previous turn, name the main runtime components.",
]


def _sum_metric(calls: list[dict[str, Any]], key: str) -> int | float:
    total: int | float = 0
    for call in calls:
        value = call.get("performance", {}).get(key)
        if isinstance(value, (int, float)):
            total += value
    return total


def summarize_model_calls(events: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [
        event.get("payload", {})
        for event in events
        if event.get("event_type") == "model_call_completed"
    ]
    prefill_tokens = _sum_metric(calls, "prefill_tokens")
    prefill_duration_ms = _sum_metric(calls, "prefill_duration_ms")
    decode_tokens = _sum_metric(calls, "decode_tokens")
    decode_duration_ms = _sum_metric(calls, "decode_duration_ms")

    summary: dict[str, Any] = {
        "model_calls": len(calls),
        "prompt_tokens": sum(int(call.get("prompt_tokens", 0)) for call in calls),
        "completion_tokens": sum(
            int(call.get("completion_tokens", 0)) for call in calls
        ),
        "model_duration_ms": sum(int(call.get("duration_ms", 0)) for call in calls),
        "prefill_tokens": prefill_tokens,
        "prefill_duration_ms": prefill_duration_ms,
        "decode_tokens": decode_tokens,
        "decode_duration_ms": decode_duration_ms,
        "first_call": calls[0] if calls else None,
        "calls": calls,
    }
    if prefill_duration_ms > 0:
        summary["prefill_tokens_per_second"] = round(
            prefill_tokens * 1000 / prefill_duration_ms,
            2,
        )
    if decode_duration_ms > 0:
        summary["decode_tokens_per_second"] = round(
            decode_tokens * 1000 / decode_duration_ms,
            2,
        )
    return summary


def summarize_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join durable tool start/completion events and classify visible errors."""
    calls: dict[int, dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload", {})
        sequence = payload.get("seq")
        if not isinstance(sequence, int):
            continue
        if event.get("event_type") == "tool_started":
            calls[sequence] = {
                "seq": sequence,
                "tool": payload.get("tool"),
                "status": "started",
            }
        elif event.get("event_type") == "tool_completed":
            call = calls.setdefault(
                sequence,
                {"seq": sequence, "tool": payload.get("tool")},
            )
            output = str(payload.get("output_preview", ""))
            call["status"] = (
                "error"
                if output.startswith(("❌", "Error [", "ERROR:"))
                else "ok"
            )
            call["output_chars"] = len(output)
            call["output_sha256"] = hashlib.sha256(
                output.encode("utf-8")
            ).hexdigest()
    return [calls[sequence] for sequence in sorted(calls)]


def _ratio(warm: int | float | None, cold: int | float | None) -> float | None:
    if not isinstance(warm, (int, float)) or not isinstance(cold, (int, float)):
        return None
    if cold <= 0:
        return None
    return round(warm / cold, 3)


def compare_first_two_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    if len(turns) < 2:
        return {}
    cold = turns[0]
    warm = turns[1]
    cold_summary = cold["model_summary"]
    warm_summary = warm["model_summary"]
    cold_first = cold_summary.get("first_call") or {}
    warm_first = warm_summary.get("first_call") or {}
    cold_performance = cold_first.get("performance", {})
    warm_performance = warm_first.get("performance", {})

    return {
        "stable_prefix_match": bool(
            cold_first.get("input_prefix_hash")
            and cold_first.get("input_prefix_hash")
            == warm_first.get("input_prefix_hash")
        ),
        "warm_to_cold_http_latency": _ratio(
            warm.get("http_latency_ms"), cold.get("http_latency_ms")
        ),
        "warm_to_cold_total_prefill_duration": _ratio(
            warm_summary.get("prefill_duration_ms"),
            cold_summary.get("prefill_duration_ms"),
        ),
        "warm_to_cold_first_call_prefill_duration": _ratio(
            warm_performance.get("prefill_duration_ms"),
            cold_performance.get("prefill_duration_ms"),
        ),
        "warm_to_cold_first_call_prefill_tokens": _ratio(
            warm_performance.get("prefill_tokens"),
            cold_performance.get("prefill_tokens"),
        ),
    }


async def run_turn(
    client: httpx.AsyncClient,
    *,
    agent_url: str,
    jwt: str,
    model: str,
    session_id: str,
    prompt: str,
    turn_index: int,
    include_content: bool = False,
) -> dict[str, Any]:
    started_at = time.monotonic()
    response = await client.post(
        f"{agent_url.rstrip('/')}/v1/chat/completions",
        headers={"Authorization": f"Bearer {jwt}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "session_id": session_id,
            "temperature": 0,
        },
    )
    http_latency_ms = int((time.monotonic() - started_at) * 1000)
    response.raise_for_status()
    body = response.json()
    run_id = body.get("run_id")
    if not run_id:
        raise RuntimeError(
            "Agent response did not include run_id; update agent-server before "
            "running the warm-cache benchmark"
        )

    timeline_response = await client.get(
        f"{agent_url.rstrip('/')}/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    timeline_response.raise_for_status()
    timeline = timeline_response.json()
    events = timeline.get("events", [])
    answer = (
        ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    )
    result = {
        "turn": turn_index,
        "prompt_hash": "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "run_id": run_id,
        "request_id": body.get("request_id"),
        "http_latency_ms": http_latency_ms,
        "run_status": timeline.get("status"),
        "tool_calls": summarize_tool_calls(events),
        "model_summary": summarize_model_calls(events),
    }
    if include_content:
        result["prompt"] = prompt
        result["answer_preview"] = answer[:300]
    return result


async def run_benchmark(
    *,
    prompts: list[str],
    agent_url: str,
    jwt: str,
    model: str,
    session_id: str,
    include_content: bool = False,
) -> dict[str, Any]:
    turns = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for index, prompt in enumerate(prompts, start=1):
            turns.append(
                await run_turn(
                    client,
                    agent_url=agent_url,
                    jwt=jwt,
                    model=model,
                    session_id=session_id,
                    prompt=prompt,
                    turn_index=index,
                    include_content=include_content,
                )
            )
    return {
        "session_id": session_id,
        "model": model,
        "turns": turns,
        "comparison": compare_first_two_turns(turns),
    }


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Compare cold and warm local-agent turns in one session"
    )
    parser.add_argument("--agent-url", default=DEFAULT_AGENT_URL)
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL)
    parser.add_argument("--jwt-file", default=DEFAULT_JWT_FILE)
    parser.add_argument("--model", default="coding-agent")
    parser.add_argument("--session-id", default=f"warm-cache:{int(time.time())}")
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        help="Prompt to run; repeat at least twice (defaults to a README pair)",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="Include raw prompts and answer previews in the report",
    )
    args = parser.parse_args()

    prompts = args.prompts or DEFAULT_PROMPTS
    if len(prompts) < 2:
        parser.error("--prompt must be supplied at least twice")
    jwt = _ensure_jwt(args.jwt_file, args.auth_url)
    report = asyncio.run(
        run_benchmark(
            prompts=prompts,
            agent_url=args.agent_url,
            jwt=jwt,
            model=args.model,
            session_id=args.session_id,
            include_content=args.include_content,
        )
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    cli()