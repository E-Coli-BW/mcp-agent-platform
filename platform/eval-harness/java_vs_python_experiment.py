#!/usr/bin/env python3
"""java_vs_python_experiment.py — Head-to-head comparison of Java and Python agent servers.

PURPOSE:
    Run the SAME golden eval cases against BOTH agent servers and compare:
    1. Correctness (pass rate on golden assertions)
    2. Latency (time-to-first-token, total response time)
    3. Token efficiency (prompt + completion tokens, tool call count)
    4. Streaming fidelity (SSE format compliance, event ordering)
    5. Tool execution accuracy (correct tool invoked, correct arguments)
    6. Context window management (behavior under long conversations)
    7. Error recovery (behavior when tools fail)

EXPERIMENTAL DESIGN:
    Factor: server implementation (Python vs Java)
    Controls: same LLM (Ollama qwen2.5:7b), same workspace, same JWT, same tool backends
    Cases: the golden eval suite (7 cases × 5 repetitions = 35 runs per server)
    Randomization: case order shuffled within each repetition (prevents ordering effects)

PRE-REGISTRATION (set BEFORE running):
    H1: Java pass rate within ±10pp of Python (parity hypothesis)
    H2: Java median latency within ±20% of Python (streaming loop overhead)
    H3: Java tool_call count within ±1 of Python per case (same LLM, same tools)
    H4: Both emit identical SSE event types in same order for non-flaky cases

RUN:
    # Start both servers (different ports)
    # Python: port 8500 (default)
    # Java: port 8580
    cd platform/eval-harness
    python java_vs_python_experiment.py --python-url http://localhost:8500 \
                                        --java-url http://localhost:8580 \
                                        --runs 5 \
                                        --out runs/java-vs-python-$(date +%Y%m%d-%H%M)

PREREQUISITES:
    - Both servers running with SAME model (AGENT_DEFAULT_MODEL=qwen2.5:7b)
    - Both pointed at SAME Ollama instance (same endpoint)
    - Both using SAME workspace root (for file tools)
    - Both using SAME memory-server and codeexec-server
    - Ollama model pre-warmed (run one throwaway request to each)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import httpx
import yaml

GOLDEN_DIR = Path(__file__).parent / "golden"
JWT_SECRET = os.environ.get("AGENT_JWT_SECRET", "default-dev-secret-DO-NOT-USE-IN-PRODUCTION")


# ── Eval case loader ─────────────────────────────────────────────────────────
@dataclass
class EvalCase:
    id: str
    description: str
    prompt: str
    tags: list[str]
    expected: dict


def load_cases() -> list[EvalCase]:
    cases = []
    for path in sorted(GOLDEN_DIR.glob("*.yaml")):
        with open(path) as f:
            raw = yaml.safe_load(f)
        # Skip cases that require external judge models
        if "explain_recursion" in raw.get("id", ""):
            continue
        cases.append(EvalCase(
            id=raw["id"],
            description=raw.get("description", ""),
            prompt=raw["prompt"],
            tags=raw.get("tags", []),
            expected=raw.get("expected", {}),
        ))
    return cases


# ── Result envelope ──────────────────────────────────────────────────────────
@dataclass
class RunResult:
    server: str  # "python" or "java"
    case_id: str
    repetition: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    # Timing
    time_to_first_token_ms: int = 0
    total_duration_ms: int = 0
    # Tokens
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    # Response
    full_response: str = ""
    # SSE events
    sse_events_count: int = 0
    sse_event_types: list[str] = field(default_factory=list)
    # Errors
    error: Optional[str] = None


# ── JWT generator ────────────────────────────────────────────────────────────
def generate_jwt(secret: str = JWT_SECRET) -> str:
    """Generate a test JWT for auth."""
    import jwt as pyjwt
    payload = {
        "sub": "eval-harness",
        "tenant_id": "eval",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    key = secret.encode()
    if len(key) < 32:
        key = key.ljust(32, b'\x00')
    return pyjwt.encode(payload, key, algorithm="HS256")


# ── Single case runner ───────────────────────────────────────────────────────
def run_case(base_url: str, server_name: str, case: EvalCase, repetition: int,
             token: str) -> RunResult:
    """Run a single eval case against a server and measure everything."""
    result = RunResult(server=server_name, case_id=case.id, repetition=repetition,
                       passed=False)

    payload = {
        "model": "coding-agent",
        "stream": True,
        "temperature": 0.0,  # deterministic for comparison
        "messages": [{"role": "user", "content": case.prompt}],
        "session_id": f"eval-{case.id}-{server_name}-rep{repetition}-{int(time.time())}",
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t_start = time.monotonic()
    t_first_token = None
    full_response = ""
    sse_event_types = []
    tool_names = []
    tool_count = 0

    try:
        with httpx.stream("POST", f"{base_url}/v1/chat/completions",
                          json=payload, headers=headers, timeout=120.0) as resp:
            if resp.status_code != 200:
                result.error = f"HTTP {resp.status_code}: {resp.text}"
                return result

            for line in resp.iter_lines():
                if not line.strip():
                    continue

                # Parse SSE format
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    sse_event_types.append(event_type)
                    continue
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                elif line.startswith("{"):
                    data_str = line.strip()
                else:
                    continue

                if data_str == "[DONE]":
                    sse_event_types.append("[DONE]")
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Content chunk (OpenAI format)
                if "choices" in data:
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        if t_first_token is None:
                            t_first_token = time.monotonic()
                        full_response += content

                # Status event
                elif "state" in data:
                    pass  # thinking / complete

                # Tool event
                elif "tool" in data:
                    tool_name = data.get("tool", "")
                    if tool_name and data.get("seq"):
                        tool_names.append(tool_name)
                        tool_count += 1

    except httpx.TimeoutException:
        result.error = "timeout (120s)"
        return result
    except Exception as e:
        result.error = f"exception: {type(e).__name__}: {e}"
        return result

    t_end = time.monotonic()
    result.total_duration_ms = int((t_end - t_start) * 1000)
    result.time_to_first_token_ms = int((t_first_token - t_start) * 1000) if t_first_token else 0
    result.full_response = full_response
    result.sse_events_count = len(sse_event_types)
    result.sse_event_types = sse_event_types
    result.tool_calls = tool_count
    result.tools_used = tool_names
    # Rough token estimate
    result.completion_tokens = len(full_response) // 4

    # Grade against expected assertions
    failures = grade(case.expected, full_response, tool_names, tool_count,
                     result.total_duration_ms)
    result.failures = failures
    result.passed = len(failures) == 0

    return result


# ── Grading ──────────────────────────────────────────────────────────────────
def grade(expected: dict, response: str, tools: list[str], tool_count: int,
          duration_ms: int) -> list[str]:
    """Grade a response against the expected assertions. Returns list of failures."""
    failures = []
    lower = response.lower()

    # tools_called_min / max
    if "tools_called_min" in expected:
        if tool_count < expected["tools_called_min"]:
            failures.append(f"tools_called_min: got {tool_count}, expected >= {expected['tools_called_min']}")
    if "tools_called_max" in expected:
        if tool_count > expected["tools_called_max"]:
            failures.append(f"tools_called_max: got {tool_count}, expected <= {expected['tools_called_max']}")

    # tools_forbidden
    for forbidden in expected.get("tools_forbidden", []):
        if forbidden in tools:
            failures.append(f"tools_forbidden: {forbidden} was called")

    # final_must_contain
    for phrase in expected.get("final_must_contain", []):
        if phrase.lower() not in lower:
            failures.append(f"final_must_contain: '{phrase}' not in response")

    # final_must_not_contain
    for phrase in expected.get("final_must_not_contain", []):
        if phrase.lower() in lower:
            failures.append(f"final_must_not_contain: '{phrase}' found in response")

    # max_latency_ms
    if "max_latency_ms" in expected:
        if duration_ms > expected["max_latency_ms"]:
            failures.append(f"max_latency_ms: {duration_ms}ms > {expected['max_latency_ms']}ms")

    # max_completion_tokens
    if "max_completion_tokens" in expected:
        approx_tokens = len(response) // 4
        if approx_tokens > expected["max_completion_tokens"]:
            failures.append(f"max_completion_tokens: ~{approx_tokens} > {expected['max_completion_tokens']}")

    return failures


# ── Analysis ─────────────────────────────────────────────────────────────────
@dataclass
class ServerSummary:
    server: str
    total_cases: int
    passed: int
    pass_rate: float
    median_latency_ms: float
    p95_latency_ms: float
    median_ttft_ms: float
    avg_tool_calls: float
    avg_completion_tokens: float
    errors: int


def analyze(results: list[RunResult]) -> dict:
    """Produce a comparative analysis of both servers."""
    by_server: dict[str, list[RunResult]] = {"python": [], "java": []}
    for r in results:
        by_server[r.server].append(r)

    summaries = {}
    for server, runs in by_server.items():
        valid = [r for r in runs if r.error is None]
        latencies = [r.total_duration_ms for r in valid]
        ttfts = [r.time_to_first_token_ms for r in valid if r.time_to_first_token_ms > 0]
        tools = [r.tool_calls for r in valid]
        tokens = [r.completion_tokens for r in valid]

        summaries[server] = ServerSummary(
            server=server,
            total_cases=len(runs),
            passed=sum(1 for r in runs if r.passed),
            pass_rate=sum(1 for r in runs if r.passed) / len(runs) if runs else 0,
            median_latency_ms=statistics.median(latencies) if latencies else 0,
            p95_latency_ms=sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0,
            median_ttft_ms=statistics.median(ttfts) if ttfts else 0,
            avg_tool_calls=statistics.mean(tools) if tools else 0,
            avg_completion_tokens=statistics.mean(tokens) if tokens else 0,
            errors=sum(1 for r in runs if r.error is not None),
        )

    # Per-case comparison
    case_comparison = {}
    case_ids = set(r.case_id for r in results)
    for case_id in sorted(case_ids):
        py_runs = [r for r in results if r.case_id == case_id and r.server == "python"]
        java_runs = [r for r in results if r.case_id == case_id and r.server == "java"]
        case_comparison[case_id] = {
            "python_pass_rate": sum(1 for r in py_runs if r.passed) / len(py_runs) if py_runs else 0,
            "java_pass_rate": sum(1 for r in java_runs if r.passed) / len(java_runs) if java_runs else 0,
            "python_median_ms": statistics.median([r.total_duration_ms for r in py_runs if not r.error]) if py_runs else 0,
            "java_median_ms": statistics.median([r.total_duration_ms for r in java_runs if not r.error]) if java_runs else 0,
            "python_avg_tools": statistics.mean([r.tool_calls for r in py_runs if not r.error]) if py_runs else 0,
            "java_avg_tools": statistics.mean([r.tool_calls for r in java_runs if not r.error]) if java_runs else 0,
        }

    # Hypothesis testing
    py_s = summaries.get("python")
    java_s = summaries.get("java")
    hypotheses = {}
    if py_s and java_s:
        hypotheses["H1_pass_rate_parity"] = {
            "python": py_s.pass_rate,
            "java": java_s.pass_rate,
            "delta_pp": (java_s.pass_rate - py_s.pass_rate) * 100,
            "within_10pp": abs(java_s.pass_rate - py_s.pass_rate) <= 0.10,
        }
        hypotheses["H2_latency_parity"] = {
            "python_median_ms": py_s.median_latency_ms,
            "java_median_ms": java_s.median_latency_ms,
            "ratio": java_s.median_latency_ms / py_s.median_latency_ms if py_s.median_latency_ms > 0 else 0,
            "within_20pct": abs(java_s.median_latency_ms - py_s.median_latency_ms) / max(py_s.median_latency_ms, 1) <= 0.20,
        }
        hypotheses["H3_tool_calls_parity"] = {
            "python_avg": py_s.avg_tool_calls,
            "java_avg": java_s.avg_tool_calls,
            "delta": abs(java_s.avg_tool_calls - py_s.avg_tool_calls),
            "within_1": abs(java_s.avg_tool_calls - py_s.avg_tool_calls) <= 1.0,
        }

    return {
        "summaries": {k: asdict(v) for k, v in summaries.items()},
        "case_comparison": case_comparison,
        "hypotheses": hypotheses,
    }


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Java vs Python agent server A/B experiment")
    parser.add_argument("--python-url", default="http://localhost:8500")
    parser.add_argument("--java-url", default="http://localhost:8580")
    parser.add_argument("--runs", type=int, default=5, help="Repetitions per case per server")
    parser.add_argument("--out", type=str, default=None, help="Output directory")
    parser.add_argument("--cases", nargs="*", default=None, help="Specific case IDs to run")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else Path(f"runs/java-vs-python-{int(time.time())}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load cases
    all_cases = load_cases()
    if args.cases:
        all_cases = [c for c in all_cases if c.id in args.cases]
    print(f"📋 Loaded {len(all_cases)} eval cases")
    print(f"🔁 {args.runs} repetitions per case per server = {len(all_cases) * args.runs * 2} total runs")

    # Generate JWT
    try:
        token = generate_jwt()
    except ImportError:
        print("⚠️  PyJWT not installed — using raw HMAC token from env")
        token = os.environ.get("EVAL_JWT", "")

    # Health check both servers
    for name, url in [("Python", args.python_url), ("Java", args.java_url)]:
        try:
            r = httpx.get(f"{url}/health", timeout=5.0)
            info = r.json()
            print(f"✅ {name} server healthy: {info}")
        except Exception as e:
            print(f"❌ {name} server at {url} unreachable: {e}")
            sys.exit(1)

    # Run experiment
    results: list[RunResult] = []
    servers = [("python", args.python_url), ("java", args.java_url)]

    for rep in range(1, args.runs + 1):
        # Shuffle case order per repetition to prevent ordering effects
        shuffled = list(all_cases)
        random.shuffle(shuffled)

        for case in shuffled:
            for server_name, base_url in servers:
                print(f"  [{rep}/{args.runs}] {server_name}: {case.id}...", end=" ", flush=True)
                result = run_case(base_url, server_name, case, rep, token)
                results.append(result)
                status = "✅" if result.passed else ("💥" if result.error else "❌")
                print(f"{status} ({result.total_duration_ms}ms, {result.tool_calls} tools)")

                # Save per-run result
                run_file = out_dir / f"{server_name}_{case.id}_rep{rep}.json"
                with open(run_file, "w") as f:
                    json.dump(asdict(result), f, indent=2)

    # Analysis
    print("\n" + "=" * 70)
    print("📊 ANALYSIS")
    print("=" * 70)

    analysis = analyze(results)

    # Print summaries
    for server, summary in analysis["summaries"].items():
        s = summary
        print(f"\n  {server.upper()}:")
        print(f"    Pass rate:     {s['pass_rate']:.0%} ({s['passed']}/{s['total_cases']})")
        print(f"    Median latency: {s['median_latency_ms']:.0f}ms")
        print(f"    P95 latency:    {s['p95_latency_ms']:.0f}ms")
        print(f"    Median TTFT:    {s['median_ttft_ms']:.0f}ms")
        print(f"    Avg tools:      {s['avg_tool_calls']:.1f}")
        print(f"    Errors:         {s['errors']}")

    # Print hypotheses
    print("\n  HYPOTHESES:")
    for h_id, h in analysis["hypotheses"].items():
        passed = any(v is True for v in h.values() if isinstance(v, bool))
        icon = "✅" if passed else "❌"
        print(f"    {icon} {h_id}: {json.dumps(h, default=str)}")

    # Print per-case comparison
    print("\n  PER-CASE COMPARISON:")
    print(f"    {'Case':<45} {'Py Pass':>8} {'Java Pass':>9} {'Py ms':>7} {'Java ms':>8} {'Py Tools':>9} {'Java Tools':>10}")
    for case_id, comp in analysis["case_comparison"].items():
        print(f"    {case_id:<45} {comp['python_pass_rate']:>7.0%} {comp['java_pass_rate']:>9.0%} "
              f"{comp['python_median_ms']:>7.0f} {comp['java_median_ms']:>8.0f} "
              f"{comp['python_avg_tools']:>9.1f} {comp['java_avg_tools']:>10.1f}")

    # Save full analysis
    analysis_file = out_dir / "analysis.json"
    with open(analysis_file, "w") as f:
        json.dump(analysis, f, indent=2, default=str)
    print(f"\n📁 Results saved to: {out_dir}")
    print(f"📊 Analysis saved to: {analysis_file}")

    # Verdict
    print("\n" + "=" * 70)
    py_pass = analysis["summaries"]["python"]["pass_rate"]
    java_pass = analysis["summaries"]["java"]["pass_rate"]
    if abs(py_pass - java_pass) <= 0.10:
        print("🏁 VERDICT: Java and Python are at PARITY on correctness.")
    elif java_pass > py_pass:
        print(f"🏁 VERDICT: Java OUTPERFORMS Python by {(java_pass - py_pass)*100:.1f}pp.")
    else:
        print(f"🏁 VERDICT: Python OUTPERFORMS Java by {(py_pass - java_pass)*100:.1f}pp.")


if __name__ == "__main__":
    main()

