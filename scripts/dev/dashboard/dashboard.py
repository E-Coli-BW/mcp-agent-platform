"""dashboard.py — local ops console for the MCP dev stack.

WHY THIS EXISTS
---------------
During development the failure mode is almost always "which of 5 logs do I
read, and what should I have noticed in it?". This dashboard:

1. Tails every component's log file from $MCP_RUN_DIR/*.log via SSE,
   so a developer doesn't have to ``tail -F`` 4 terminals.
2. Probes each component's /actuator/health (or /health) every 3s and
   surfaces a colored badge — the "is it even up?" question.
3. Extracts structured signals from logs (status code, prompt tokens, tool
   call counts, JWT verification failures) and shows them as metrics + a
   recent-events timeline.
4. Provides one-click reproduction buttons: "log in", "send memory_set",
   "send code_run". Each button shows the exact curl invocation it issued
   so you can replay it in your own shell.

WHAT THIS DOES *NOT* DO
-----------------------
Agent quality (trajectory accuracy, hallucination rate, tool-selection
correctness) is **eval**, not ops. It needs a reference truth and lives
in a separate harness. This dashboard answers two questions only:

    (a) Is the stack healthy? — RT, errors, throughput, mem (axis 1)
    (b) What is it costing?   — tokens, $, context pressure (axis 2)

Trajectory quality is axis 3 → see the `eval/` harness, not this file.

WHY A SINGLE FILE
-----------------
The point is to be runnable WITHOUT extra setup. The agent-server's venv
already has FastAPI + uvicorn. We add nothing. HTML/CSS/JS are inlined
because shipping a tiny SPA build pipeline would defeat the purpose.

WHY NOT REUSE THE EXISTING REACT FRONTEND
-----------------------------------------
That frontend is a chat UI, not an ops view. It also breaks easily (we hit
empty ``package.json`` earlier this session). Keeping this independent
means it works even when the React frontend is broken — which is exactly
when you need it most.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse

# ── Configuration ────────────────────────────────────────────────────
MCP_RUN_DIR = Path(os.environ.get("MCP_RUN_DIR", str(Path.cwd().parent.parent / ".dev-run")))
MCP_ROOT = Path(os.environ.get("MCP_ROOT", str(Path.cwd().parent.parent.parent)))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "9090"))

# ── Eval harness integration ─────────────────────────────────────────
# The eval-run / eval-suite-bfcl CLIs write summaries to:
#   $MCP_ROOT/platform/eval-harness/runs/latest.json       (agent-loop suite)
#   $MCP_ROOT/platform/eval-harness/runs/latest-bfcl.json  (BFCL suite)
# We read those files on-demand for the /api/eval endpoint. No file
# watcher — just re-read on each request (cheap; the files are KB-scale).
EVAL_RUNS_DIR = Path(os.environ.get(
    "EVAL_RUNS_DIR",
    str(MCP_ROOT / "platform" / "eval-harness" / "runs"),
))

# Components and their default health endpoints.
# Mirrors the bash registry in scripts/dev/common.sh. (We deliberately omit
# `dashboard` itself — it's the thing serving this page; if you can read
# this, the dashboard is up.)
#
# `frontend` is the React chat UI at :3000. Vite's `/` returns 200 as
# soon as the dev server is listening (even before the bundle compiles),
# which is the level of "alive" we want to surface here. The dashboard's
# expected-component logic distinguishes "DOWN because crashed" from
# "DOWN because this scenario didn't start it" via the marker file, so
# minimal/with-tools scenarios won't show frontend as red.
COMPONENTS = [
    ("auth",       8090, "/auth/jwks"),
    ("memory",     8180, "/actuator/health"),
    ("codeexec",   8380, "/actuator/health"),
    ("filesearch", 8280, "/actuator/health"),
    ("agent",      8580, "/health"),
    ("frontend",   3000, "/"),
]

# ── In-memory recent-events ring ─────────────────────────────────────
# Kept small so the dashboard never holds GBs in RAM. The full log is on
# disk anyway; this is just for the "Recent Activity" pane.
RECENT_EVENTS: collections.deque = collections.deque(maxlen=200)

# Per-component metrics derived from log scraping.
#
# Design notes:
# - `latencies` is a bounded deque so percentile computation stays O(N log N)
#   over a small N. We do NOT use a full t-digest — the dashboard is for
#   eyeballing, not for SLO enforcement.
# - `error_timestamps` is a deque of unix ts'es; we recompute "errors in
#   last 5 min" on demand by trimming the head. This is the cheapest way
#   to get a real sliding window without a background sweeper task.
# - `sessions` is a {session_id: last_seen_ts} map; the active count is
#   "entries with last_seen_ts > now-10min". Bounded to 1000 entries
#   (oldest evicted) so a misbehaving client can't OOM us.
METRICS: dict[str, dict] = {
    name: {
        "errors_5m": 0,           # recomputed on read from error_timestamps
        "last_error": None,
        "last_seen": None,
        # ── agent-server-specific (populated only for "agent") ──
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls": 0,
        "requests": 0,
        "cost_usd": 0.0,           # cumulative — from `total_cost=$X.XXXX`
        "max_prompt_tokens": 0,    # context-window pressure indicator
        "p50_ms": 0,
        "p95_ms": 0,
        "p99_ms": 0,
        # ── backend-specific (memory, codeexec, …) ──
        # `tools` is a per-tool aggregate scraped from AUDIT lines:
        #   tool_name → {"calls": int, "errors": int, "p95_ms": int, "last_ms": int}
        "tools": {},
    }
    for name, _, _ in COMPONENTS
}

# Out-of-band working state (NOT exposed directly — we derive METRICS from
# these on every read so the snapshot is always consistent).
_LATENCIES: dict[str, collections.deque] = {
    name: collections.deque(maxlen=200) for name, _, _ in COMPONENTS
}
_ERROR_TS: dict[str, collections.deque] = {
    name: collections.deque(maxlen=500) for name, _, _ in COMPONENTS
}
_SESSIONS: dict[str, float] = {}        # session_id → last_seen_ts (agent only)
_TOOL_LAT: dict[tuple[str, str], collections.deque] = {}  # (component,tool) → deque of ms
# Rolling per-component HTTP probe history for plumbing-error visibility.
# Cap to 100 samples (~5 min at 3s cadence).
_PROBE_HISTORY: dict[str, collections.deque] = {
    name: collections.deque(maxlen=100) for name, _, _ in COMPONENTS
}
_EVAL_PROCESS: subprocess.Popen | None = None
PROBE_STATE_FILE = MCP_RUN_DIR / "probe-state.json"


def _read_probe_state() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(PROBE_STATE_FILE.read_text()) if PROBE_STATE_FILE.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_probe_state(kind: str, state: dict[str, Any]) -> None:
    current = _read_probe_state()
    current[kind] = {**state, "updated_at": time.time()}
    PROBE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROBE_STATE_FILE.write_text(json.dumps(current, ensure_ascii=False))

# ── Fleet tracker (P1 #4) ─────────────────────────────────────────────
# A "fleet" is the set of audit rows sharing the same root_session_id —
# i.e. one user request and every subagent it spawned. We bucket recent
# audit rows by root_session and let the UI render an indented timeline
# so you can see the spawn shape at a glance.
#
# Why a chronological-indented timeline instead of a true tree?
#   The audit row carries (root, immediate_parent, depth) but NOT the
#   row's own session_id, so we can't draw real parent→child edges from
#   audit alone. Indented chronological view side-steps the problem and
#   is arguably MORE useful for debugging — you see "what happened next"
#   plus depth at the same time. If we ever want a real tree, add
#   X-Self-Session-Id to the wire format (one-line Python change, one-line
#   Java change, one new test on each side — both sides have the pinning
#   test that will fail if you forget).
#
# Why bounded?
#   A dev box may run thousands of audit rows over a session. We keep
#   the last N fleets by recency, and each fleet keeps its last M rows.
#   Tight bounds because this is a dev tool, not an SRE system.
_FLEET_MAX = 50          # number of distinct root_sessions to retain
_FLEET_ROWS_MAX = 200    # max audit rows kept per fleet
# root_session_id → {
#     "first_seen": ts, "last_seen": ts,
#     "rows": deque[FleetRow]
# }
_FLEETS: collections.OrderedDict[str, dict] = collections.OrderedDict()


def _record_fleet_row(
    *,
    root: str,
    parent: str,
    depth: int,
    component: str,
    tool: str,
    duration_ms: int,
    status: str,
    ts: float,
) -> None:
    """Append one audit row to its fleet bucket; evict oldest fleet if over cap.

    Called from _ingest_line when the AUDIT regex captures a lineage triple.
    All three lineage fields are required (the regex makes them an
    all-or-nothing group), so by the time we get here we have a real
    root_session — not the "-/-/0" sentinel.

    NOTE: we filter out the sentinel ("-") at the call site, not here,
    because a future caller might legitimately want to record a sentinel-
    rooted "fleet" of one row for visibility. Keep this fn simple.
    """
    fleet = _FLEETS.get(root)
    if fleet is None:
        # New fleet — evict oldest if at cap. OrderedDict + move_to_end
        # gives us LRU semantics for free.
        if len(_FLEETS) >= _FLEET_MAX:
            _FLEETS.popitem(last=False)
        fleet = {
            "first_seen": ts,
            "last_seen": ts,
            "rows": collections.deque(maxlen=_FLEET_ROWS_MAX),
        }
        _FLEETS[root] = fleet
    else:
        # Existing fleet — bump it to the most-recent end of the LRU.
        _FLEETS.move_to_end(root)

    fleet["last_seen"] = ts
    fleet["rows"].append({
        "ts": ts,
        "depth": depth,
        "parent_session": parent,
        "component": component,
        "tool": tool,
        "duration_ms": duration_ms,
        "status": status,
    })


def _fleet_summary() -> list[dict]:
    """Build a UI-friendly list of fleets, newest first.

    Each entry has aggregates (call count, error count, total wall time,
    max depth) so the UI can render a one-line summary without iterating
    the full rows array. The detail view fetches rows on demand.
    """
    out = []
    # OrderedDict iteration order is insertion order; we want newest first.
    for root, fleet in reversed(_FLEETS.items()):
        rows = list(fleet["rows"])
        if not rows:
            continue
        # Aggregates over what we have (truncation-aware: if rows were
        # evicted, counts are lower bounds — that's fine, the UI calls
        # this out via the "rows kept" badge).
        errors = sum(1 for r in rows if r["status"] != "OK")
        max_depth = max(r["depth"] for r in rows)
        # Wall time approximated as last_seen - first_seen; fine for a UI
        # gauge, NOT for cost attribution (which would need real spans).
        wall_ms = int((fleet["last_seen"] - fleet["first_seen"]) * 1000)
        # Per-depth call counts for the sparkline.
        depth_hist: dict[int, int] = {}
        for r in rows:
            depth_hist[r["depth"]] = depth_hist.get(r["depth"], 0) + 1
        out.append({
            "root_session": root,
            "first_seen": fleet["first_seen"],
            "last_seen": fleet["last_seen"],
            "wall_ms": wall_ms,
            "calls": len(rows),
            "errors": errors,
            "max_depth": max_depth,
            "depth_histogram": depth_hist,
            "rows_truncated": len(rows) >= _FLEET_ROWS_MAX,
        })
    return out

# ── Log line classification ──────────────────────────────────────────
# Patterns we use to bucket each log line. We intentionally use simple
# substring/regex matching rather than a full structured-log parser —
# Spring Boot and Python both produce text logs, and unifying them at the
# parser layer is over-engineering for a dev tool.
_LEVEL_RE = re.compile(r"\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE)\b")
# Agent's `app.usage` logger:
#   📊 Usage: model=qwen2.5:7b prompt=924 completion=34 tools=2 cost=$0.000000 total_cost=$0.0000
_USAGE_RE = re.compile(
    r"Usage:\s*model=(\S+)\s+prompt=(\d+)\s+completion=(\d+)\s+tools=(\d+)"
    r"(?:\s+cost=\$([0-9.]+))?(?:\s+total_cost=\$([0-9.]+))?"
)
# Agent's `agent.chat` logger:
#   📤 Agent 响应 [session=default:session-XYZ, tools=2, 2973ms]: ...
_RESPONSE_RE = re.compile(
    r"Agent\s+\S+\s+\[session=([^,]+),\s+tools=(\d+),\s+(\d+)ms\]"
)
# Memory-server + codeexec AUDIT lines.
#
# Two formats supported (the lineage fields were added in P1 #3):
#
#   OLD (pre-P1#3, direct curl, non-agent clients):
#     AUDIT : tenant=default user=agent-server tool=memory_set duration=13ms status=OK
#
#   NEW (post-P1#3, agent-server requests):
#     AUDIT : tenant=default user=test tool=memory_set
#             root_session=chat-7 parent_session=chat-7 depth=0
#             duration=13ms status=OK
#
# The lineage triple is optional in the regex (non-capturing wrapper around
# (?P<root>…)(?P<parent>…)(?P<depth>…)) so we keep parsing pre-P1#3 logs
# and direct-curl traffic. When the triple is present we record it for the
# fleet tracker (see _FLEETS); when absent we skip fleet attribution and
# the row still updates the per-tool metrics as before.
_AUDIT_RE = re.compile(
    r"AUDIT[^:]*:\s*tenant=(?P<tenant>\S+)\s+user=(?P<user>\S+)\s+tool=(?P<tool>\S+)"
    r"(?:\s+root_session=(?P<root>\S+)\s+parent_session=(?P<parent>\S+)\s+depth=(?P<depth>\d+))?"
    r"\s+duration=(?P<dur>\d+)ms\s+status=(?P<status>\S+)"
)
_JWT_FAIL_RE = re.compile(r"(JWT validation failed|Authentication failed|invalid_client)")


def _level_of(line: str) -> str:
    """Classify a log line into ERROR / WARN / INFO / DEBUG.

    Falls back to INFO when nothing matches — that's the right default for
    Spring's status lines and Python's print() output.
    """
    m = _LEVEL_RE.search(line)
    if not m:
        return "INFO"
    lvl = m.group(1)
    return "WARN" if lvl == "WARNING" else lvl


def _ingest_line(component: str, line: str) -> None:
    """Update METRICS + RECENT_EVENTS from a single log line.

    Called from the per-component tailer task. Keep this CHEAP — it runs
    once per log line, possibly thousands per minute under load. We avoid
    running every regex against every line — fast `in` substring checks
    short-circuit first.
    """
    now = time.time()
    METRICS[component]["last_seen"] = now
    level = _level_of(line)

    # ── Errors (timestamped for true sliding window) ──────────────────
    if level == "ERROR" or "Exception" in line or _JWT_FAIL_RE.search(line):
        _ERROR_TS[component].append(now)
        METRICS[component]["last_error"] = line.strip()[:300]

    # ── Agent-specific scraping ───────────────────────────────────────
    if component == "agent":
        # Usage line: cumulative tokens + cost + context-window pressure
        if "Usage:" in line:
            m = _USAGE_RE.search(line)
            if m:
                prompt = int(m.group(2))
                METRICS["agent"]["prompt_tokens"] += prompt
                METRICS["agent"]["completion_tokens"] += int(m.group(3))
                METRICS["agent"]["tool_calls"] += int(m.group(4))
                METRICS["agent"]["requests"] += 1
                if m.group(6):  # total_cost
                    try:
                        METRICS["agent"]["cost_usd"] = float(m.group(6))
                    except ValueError:
                        pass
                if prompt > METRICS["agent"]["max_prompt_tokens"]:
                    METRICS["agent"]["max_prompt_tokens"] = prompt

        # Response line: per-request latency + session activity
        if "Agent " in line and "ms]" in line:
            rm = _RESPONSE_RE.search(line)
            if rm:
                session_id = rm.group(1).strip()
                latency_ms = int(rm.group(3))
                _LATENCIES["agent"].append(latency_ms)
                # Bounded session map: evict oldest if over 1000.
                _SESSIONS[session_id] = now
                if len(_SESSIONS) > 1000:
                    oldest = min(_SESSIONS, key=_SESSIONS.get)
                    _SESSIONS.pop(oldest, None)

    # ── Backend AUDIT scraping (memory-server, codeexec when added) ───
    if "AUDIT" in line:
        am = _AUDIT_RE.search(line)
        if am:
            tool = am.group("tool")
            duration_ms = int(am.group("dur"))
            status = am.group("status")
            tools = METRICS[component]["tools"]
            t = tools.setdefault(tool, {"calls": 0, "errors": 0, "p95_ms": 0, "last_ms": 0})
            t["calls"] += 1
            t["last_ms"] = duration_ms
            if status != "OK":
                t["errors"] += 1
            # Track latency sample for percentile computation.
            key = (component, tool)
            _TOOL_LAT.setdefault(key, collections.deque(maxlen=200)).append(duration_ms)

            # ── Fleet tracking (P1 #4): only when lineage triple is present.
            # The regex makes the lineage group all-or-nothing, so checking
            # any one of root/parent/depth is sufficient. We also filter
            # out the "-" sentinel: a row with root="-" came from a direct
            # curl with no fleet context and would just clutter the UI
            # (every direct curl would look like its own one-row "fleet").
            root = am.group("root")
            if root and root != "-":
                _record_fleet_row(
                    root=root,
                    parent=am.group("parent") or "-",
                    depth=int(am.group("depth") or 0),
                    component=component,
                    tool=tool,
                    duration_ms=duration_ms,
                    status=status,
                    ts=now,
                )

    # ── Recent-events timeline ────────────────────────────────────────
    RECENT_EVENTS.appendleft({
        "ts": now,
        "component": component,
        "level": level,
        "text": line.rstrip()[:500],
    })


def _percentile(samples: list[int], pct: float) -> int:
    """Nearest-rank percentile. Returns 0 on empty input.

    We use nearest-rank rather than linear interpolation because it's
    cheap, intuitive ("the 95th of 200 samples is the 190th"), and the
    numbers are for human eyeballing — sub-millisecond precision is
    pointless against tail-end variance.
    """
    if not samples:
        return 0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round(pct * len(s))) - 1))
    return int(s[k])


def _refresh_derived_metrics() -> None:
    """Recompute everything that's a function of bounded recent state.

    Called from /api/status on every snapshot — cheaper than maintaining
    these incrementally and easier to reason about (no drift bugs).
    """
    now = time.time()
    cutoff_5m = now - 300

    for name in METRICS:
        # Sliding-window error count: trim head of the deque to the window.
        ts_deque = _ERROR_TS[name]
        while ts_deque and ts_deque[0] < cutoff_5m:
            ts_deque.popleft()
        METRICS[name]["errors_5m"] = len(ts_deque)

        # Latency percentiles from the rolling sample (agent only for now).
        lat = list(_LATENCIES[name])
        if lat:
            METRICS[name]["p50_ms"] = _percentile(lat, 0.50)
            METRICS[name]["p95_ms"] = _percentile(lat, 0.95)
            METRICS[name]["p99_ms"] = _percentile(lat, 0.99)

        # Per-tool p95 from the per-tool latency deques.
        for tool, agg in METRICS[name]["tools"].items():
            samples = list(_TOOL_LAT.get((name, tool), ()))
            if samples:
                agg["p95_ms"] = _percentile(samples, 0.95)


def _active_sessions(window_s: int = 600) -> int:
    """Count distinct agent sessions seen in the last `window_s` seconds."""
    cutoff = time.time() - window_s
    return sum(1 for ts in _SESSIONS.values() if ts > cutoff)


# ── Scenario awareness ───────────────────────────────────────────────
# Each `scenarios/*/up.sh` writes $MCP_RUN_DIR/expected-components.txt
# listing the components it brought up. If a component is "DOWN" but NOT
# in this list, it means "not part of the current scenario" — render it
# greyed out, not red. The whole point is to avoid false alarms for the
# 95% case of `with-tools` (which deliberately excludes filesearch).
#
# We cache the file contents for `_SCENARIO_TTL` seconds so we don't
# stat() on every API call. The file is small enough that re-reading
# would be cheap, but caching lets us add a sane fallback (treat all
# components as expected) if the file is missing.
_SCENARIO_CACHE: dict = {"loaded_at": 0.0, "name": None, "components": set()}
_SCENARIO_TTL = 5.0  # seconds — file changes are rare; staleness is fine


def _load_scenario() -> tuple[str | None, set[str]]:
    """Return (scenario_name, set_of_expected_component_names).

    Falls back to (None, all_known_components) if the marker file is
    missing — that's the safe default that preserves pre-P1 behavior.
    """
    now = time.time()
    if now - _SCENARIO_CACHE["loaded_at"] < _SCENARIO_TTL:
        return _SCENARIO_CACHE["name"], _SCENARIO_CACHE["components"]

    marker = MCP_RUN_DIR / "expected-components.txt"
    name: str | None = None
    expected: set[str] = set()
    if marker.exists():
        try:
            for line in marker.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    # "# scenario=with-tools written_at=2026-05-29T01:54:50Z"
                    for tok in line[1:].split():
                        if tok.startswith("scenario="):
                            name = tok.split("=", 1)[1]
                    continue
                expected.add(line)
        except Exception:
            pass
    if not expected:
        # No marker → treat everything as expected (safe default).
        expected = {c[0] for c in COMPONENTS}
    _SCENARIO_CACHE.update({"loaded_at": now, "name": name, "components": expected})
    return name, expected


# ── Tailing tasks ────────────────────────────────────────────────────
# One asyncio task per component: read the log file as it grows. We don't
# use watchdog/inotify because logs are append-only and a simple
# "seek-then-read" loop is robust to rotation (we re-open on EOF if the
# inode changed).

async def _tail_log(component: str) -> None:
    """Background task: tail the log file forever, feeding _ingest_line."""
    path = MCP_RUN_DIR / f"{component}.log"
    pos = 0
    inode = None
    while True:
        try:
            if not path.exists():
                await asyncio.sleep(1.0)
                continue
            stat = path.stat()
            if inode is not None and stat.st_ino != inode:
                # File was rotated — re-open at start.
                pos = 0
            inode = stat.st_ino
            # If the file shrank (truncate), reset.
            if pos > stat.st_size:
                pos = 0

            with path.open("r", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            if chunk:
                for line in chunk.splitlines():
                    if line:
                        _ingest_line(component, line)
            await asyncio.sleep(0.5)
        except Exception as e:
            # Don't let a single tail blow up the dashboard.
            await asyncio.sleep(2.0)
            print(f"[tail/{component}] {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the tailers when the app starts, cancel them on shutdown."""
    tasks = [asyncio.create_task(_tail_log(name)) for name, _, _ in COMPONENTS]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(lifespan=lifespan)


# ── Health probe ─────────────────────────────────────────────────────
async def _probe_component(name: str, port: int, path: str) -> dict:
    """Single HTTP GET against a health endpoint. Returns UI-friendly dict.

    Important: timeout must be SHORT (1s). The probe runs every refresh
    cycle and a slow component must not block the entire status page.

    We also record the round-trip time and status into _PROBE_HISTORY so
    the UI can show probe latency trend + a 2xx/4xx/5xx breakdown. This
    is THE source of plumbing-health data — log-scraped errors miss
    things like "service is up but returns 500 from /health".
    """
    url = f"http://localhost:{port}{path}"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=1.0) as c:
            r = await c.get(url)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        code = r.status_code
        state = "UP" if code == 200 else f"HTTP {code}"
    except Exception:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        code = 0
        state = "DOWN"

    _PROBE_HISTORY[name].append({"ts": time.time(), "code": code, "ms": elapsed_ms})
    return {"name": name, "port": port, "url": url, "state": state, "code": code, "probe_ms": elapsed_ms}


def _probe_breakdown(name: str) -> dict:
    """Summarise the last ~5 min of probe history into 2xx/4xx/5xx/down counts."""
    hist = list(_PROBE_HISTORY[name])
    out = {"total": len(hist), "ok": 0, "client_err": 0, "server_err": 0, "down": 0, "avg_ms": 0}
    if not hist:
        return out
    total_ms = 0
    for h in hist:
        c = h["code"]
        total_ms += h["ms"]
        if c == 0:
            out["down"] += 1
        elif 200 <= c < 400:
            out["ok"] += 1
        elif 400 <= c < 500:
            out["client_err"] += 1
        elif c >= 500:
            out["server_err"] += 1
    out["avg_ms"] = total_ms // len(hist)
    return out


# ── API endpoints ────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    """One-shot snapshot: component health + derived metrics.

    Adds a per-component ``expected`` flag derived from the active
    scenario's marker file. Lets the UI render unexpected-down components
    as "not in scenario" (grey) rather than "DOWN" (red).
    """
    probes = await asyncio.gather(*[_probe_component(*c) for c in COMPONENTS])
    _refresh_derived_metrics()
    breakdowns = {p["name"]: _probe_breakdown(p["name"]) for p in probes}
    scenario_name, expected = _load_scenario()
    for p in probes:
        p["expected"] = p["name"] in expected
    return {
        "components": probes,
        "metrics": METRICS,
        "probe_breakdown": breakdowns,
        "active_sessions": _active_sessions(),
        "scenario": scenario_name,
        "expected_components": sorted(expected),
        "run_dir": str(MCP_RUN_DIR),
    }


@app.get("/api/events")
async def api_events():
    """Recent log events for the timeline pane."""
    return {"events": list(RECENT_EVENTS)}


@app.get("/api/stream")
async def api_stream():
    """SSE: push new events as they arrive.

    The browser keeps an EventSource open and we send a new event message
    whenever RECENT_EVENTS grows. Heartbeat every 15s keeps the proxy
    (if any) from killing the connection.
    """
    async def gen() -> AsyncIterator[str]:
        last_seen = 0
        while True:
            # RECENT_EVENTS is a deque keyed by insertion order — we use the
            # newest event's `ts` as a watermark.
            new_events = []
            for ev in RECENT_EVENTS:
                if ev["ts"] <= last_seen:
                    break
                new_events.append(ev)
            if new_events:
                new_events.reverse()  # oldest-first for the UI
                last_seen = max(ev["ts"] for ev in new_events)
                yield f"data: {json.dumps(new_events)}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/log/{component}")
async def api_log(component: str, tail: int = 200):
    """Return the last N lines of a component's log."""
    if component not in {c[0] for c in COMPONENTS}:
        raise HTTPException(404, f"unknown component {component}")
    path = MCP_RUN_DIR / f"{component}.log"
    if not path.exists():
        return {"lines": [], "note": "log file does not exist yet"}
    # Bounded reader: only the last ~256KB to keep things snappy.
    size = path.stat().st_size
    start = max(0, size - 256 * 1024)
    with path.open("r", errors="replace") as fh:
        fh.seek(start)
        data = fh.read()
    lines = data.splitlines()[-tail:]
    return {"lines": lines}


# ── Agent fleet view (P1 #4) ──────────────────────────────────────────
@app.get("/api/fleets")
async def api_fleets():
    """List recent agent fleets (one per root_session) with aggregates.

    The UI uses this to render the Fleet panel — one card per fleet with
    call count, error count, max depth, wall time. Click a card to fetch
    the per-row detail via /api/fleets/{root_session_id}.

    Returns at most _FLEET_MAX entries; fleets are evicted LRU.
    """
    return {"fleets": _fleet_summary()}


@app.get("/api/fleets/{root_session_id:path}")
async def api_fleet_detail(root_session_id: str):
    """Per-row detail for one fleet, oldest first (chronological timeline).

    Path uses {root_session_id:path} (not :str) so session ids that contain
    slashes — which agent-server generates for subagents
    (``chat-7/sub-abc``) — don't get URL-decoded into separate segments
    and 404 the route.

    Returns the full row list as captured (up to _FLEET_ROWS_MAX) plus
    the same aggregates as /api/fleets so the detail panel can render
    standalone without re-correlating with the list response.
    """
    fleet = _FLEETS.get(root_session_id)
    if fleet is None:
        # Not 404 — UI may have a stale fleet selected after eviction. An
        # empty rows array lets the UI render "no rows for this fleet,
        # may have been evicted" gracefully.
        return {"root_session": root_session_id, "rows": [], "evicted": True}
    rows = list(fleet["rows"])  # chronological — deque preserves append order
    errors = sum(1 for r in rows if r["status"] != "OK")
    max_depth = max((r["depth"] for r in rows), default=0)
    return {
        "root_session": root_session_id,
        "first_seen": fleet["first_seen"],
        "last_seen": fleet["last_seen"],
        "wall_ms": int((fleet["last_seen"] - fleet["first_seen"]) * 1000),
        "calls": len(rows),
        "errors": errors,
        "max_depth": max_depth,
        "rows_truncated": len(rows) >= _FLEET_ROWS_MAX,
        "rows": rows,
        "evicted": False,
    }


# ── Eval harness integration ─────────────────────────────────────────
@app.get("/api/eval")
async def api_eval():
    """Return the latest eval-run + BFCL summaries (if any).

    Reads `latest.json` and `latest-bfcl.json` written by the harness
    CLIs. No locking — these files are KB-scale and written atomically
    (write-then-rename would be safer but the harness is single-writer
    so concurrent reads are fine in practice).
    """
    out: dict[str, Any] = {"agent": None, "smoke": None, "bfcl": None, "history": [], "runs_dir": str(EVAL_RUNS_DIR)}
    for key, fname in [("agent", "latest.json"), ("bfcl", "latest-bfcl.json")]:
        p = EVAL_RUNS_DIR / fname
        if not p.exists():
            continue
        try:
            out[key] = json.loads(p.read_text())
            out[key]["_mtime"] = p.stat().st_mtime
        except Exception as e:
            out[key] = {"error": f"failed to read {fname}: {e}"}
    smoke_path = EVAL_RUNS_DIR / "smoke" / "latest.json"
    if smoke_path.exists():
        try:
            out["smoke"] = json.loads(smoke_path.read_text())
            out["smoke"]["_mtime"] = smoke_path.stat().st_mtime
            out["smoke"]["run_type"] = "smoke"
        except (OSError, json.JSONDecodeError) as exc:
            out["smoke"] = {"error": f"failed to read smoke eval: {exc}"}
    latest_agent = out.get("agent")
    if isinstance(latest_agent, dict) and not latest_agent.get("error"):
        failures = collections.Counter()
        for case in latest_agent.get("cases", []):
            for score in case.get("scores", []):
                if not score.get("passed", False):
                    failures[str(score.get("name", "unknown"))] += 1
        out["agent_failures"] = dict(failures)
    latest_smoke = out.get("smoke")
    if isinstance(latest_smoke, dict) and not latest_smoke.get("error"):
        failures = collections.Counter()
        for case in latest_smoke.get("cases", []):
            for score in case.get("scores", []):
                if not score.get("passed", False):
                    failures[str(score.get("name", "unknown"))] += 1
        out["smoke_failures"] = dict(failures)
    # Keep a small chronological history for trend reading. The harness may
    # store runs below experiment/cell directories, so discover summaries by
    # filename rather than assuming one fixed layout.
    candidates: list[tuple[float, Path]] = []
    try:
        for pattern in ("latest.json", "summary.json"):
            for path in EVAL_RUNS_DIR.rglob(pattern):
                if path.is_file():
                    candidates.append((path.stat().st_mtime, path))
        for mtime, path in sorted(candidates, reverse=True)[:12]:
            try:
                item = json.loads(path.read_text())
                out["history"].append({
                    "suite": item.get("suite", "agent"),
                    "pass_rate": item.get("pass_rate"),
                    "total": item.get("total"),
                    "passed": item.get("passed"),
                    "ts": item.get("ts"),
                    "mtime": mtime,
                    "path": str(path),
                })
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    return out


@app.get("/api/feedback")
async def api_feedback():
    """Summarise persisted retrieval feedback and the current learned policy.

    The reranker writes implicit labels to the shared local JSONL file. An
    optional user-feedback JSONL file is also accepted so the dashboard can
    show explicit ratings once the chat UI starts emitting them.
    """
    reranker_log = Path.home() / ".mcp-local" / "reranker-feedback.jsonl"
    user_log = Path.home() / ".mcp-local" / "user-feedback.jsonl"
    trace_log = Path.home() / ".mcp-local" / "retrieval-traces.jsonl"

    def read_jsonl(path: Path, limit: int = 2000) -> list[dict]:
        if not path.exists():
            return []
        try:
            lines = path.read_text(errors="replace").splitlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    implicit = read_jsonl(reranker_log)
    explicit = read_jsonl(user_log)
    traces = read_jsonl(trace_log)
    labels = collections.Counter(str(item.get("label", "unknown")) for item in implicit)
    reasons = collections.Counter(str(item.get("reason") or "unspecified") for item in explicit)
    agents = collections.Counter(str(item.get("agent") or "unknown") for item in explicit)
    positive = sum(1 for item in explicit if item.get("rating") == "positive")
    linked = sum(1 for item in explicit if item.get("retrieval_trace_ids"))
    chunk_counts = [len(item.get("chunks", [])) for item in traces if isinstance(item.get("chunks"), list)]
    weights_path = Path.home() / ".mcp-local" / "reranker-weights.json"
    weights = None
    weights_updated_at = None
    if weights_path.exists():
        try:
            data = json.loads(weights_path.read_text())
            weights = data.get("weights")
            weights_updated_at = data.get("updated_at")
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "implicit": {"samples": len(implicit), "labels": dict(labels), "path": str(reranker_log)},
        "explicit": {
            "samples": len(explicit), "positive": positive,
            "positive_rate": round(positive / len(explicit), 3) if explicit else None,
            "reasons": dict(reasons), "agents": dict(agents), "path": str(user_log),
        },
        "retrieval": {
            "traces": len(traces), "avg_chunks": round(sum(chunk_counts) / len(chunk_counts), 2) if chunk_counts else None,
            "path": str(trace_log), "linked_feedback": linked,
            "link_rate": round(linked / len(explicit), 3) if explicit else None,
        },
        "reranker": {"weights": weights, "updated_at": weights_updated_at, "ready_to_retrain": len(implicit) >= 5 and len(labels) > 1},
        "quality": _read_rag_quality(),
        "recent": (implicit[-5:] + explicit[-5:])[-10:],
    }


def _read_rag_quality() -> dict:
    path = Path.home() / ".mcp-local" / "customer-qa-retrieval.json"
    if not path.exists():
        return {"available": False}
    try:
        return {"available": True, **json.loads(path.read_text())}
    except (OSError, json.JSONDecodeError):
        return {"available": False}


@app.post("/api/probe/login")
async def api_probe_login():
    """One-click: signup-if-needed + login + cache JWT.

    Mirrors `ensure_test_user` in common.sh. Returning the curl command we
    issued is a deliberate teaching move — the dashboard shouldn't be a
    black box.
    """
    auth = "http://localhost:8090"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"{auth}/auth/signup", json={
                "username": "test", "password": "testpass1234",
                "tenant_id": "default", "email": "test@test.com",
            })  # ignore failures — user may already exist
            r = await c.post(f"{auth}/auth/login", json={
                "username": "test", "password": "testpass1234",
            })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"ok": False, "error": r.text}, status_code=r.status_code)
    jwt = r.json().get("access_token", "")
    (MCP_RUN_DIR / "jwt.txt").write_text(jwt)
    return {
        "ok": True,
        "jwt_len": len(jwt),
        "saved_to": str(MCP_RUN_DIR / "jwt.txt"),
        "curl": (
            "curl -sS -X POST http://localhost:8090/auth/login "
            "-H 'Content-Type: application/json' "
            "-d '{\"username\":\"test\",\"password\":\"testpass1234\"}'"
        ),
    }


@app.post("/api/probe/eval")
async def api_probe_eval():
    """Start the small default agent eval from the analysis workspace.

    This is intentionally a fixed, bounded command: the dashboard can
    reproduce the documented smoke evaluation but cannot execute arbitrary
    shell input supplied by a browser.
    """
    global _EVAL_PROCESS
    if _EVAL_PROCESS is not None and _EVAL_PROCESS.poll() is None:
        return JSONResponse({"ok": True, "running": True, "pid": _EVAL_PROCESS.pid, "instruction": "An eval is already running."}, status_code=202)
    binary = MCP_ROOT / "platform" / "agent-server" / ".venv" / "bin" / "eval-run"
    if not binary.exists():
        return JSONResponse({"ok": False, "error": f"eval-run not found: {binary}"}, status_code=503)
    # Keep the eval identity aligned with the running scenario. The runner's
    # historical default points at .dev-run, while this dashboard may be
    # serving .dev-run-latest (or another isolated scenario directory).
    jwt_path = MCP_RUN_DIR / "jwt.txt"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                "http://localhost:8090/auth/signup",
                json={"username": "test", "password": "testpass1234", "tenant_id": "default", "email": "test@test.com"},
            )
            login = await client.post(
                "http://localhost:8090/auth/login",
                json={"username": "test", "password": "testpass1234"},
            )
        if login.status_code != 200:
            return JSONResponse({"ok": False, "error": f"default eval identity login failed: HTTP {login.status_code} {login.text[:200]}"}, status_code=502)
        jwt_path.parent.mkdir(parents=True, exist_ok=True)
        jwt_path.write_text(login.json().get("access_token", ""))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"default eval identity unavailable: {exc}"}, status_code=502)
    try:
        eval_log = MCP_RUN_DIR / "eval-run.log"
        smoke_cases = ["no_tool_simple_chat", "code_run_basic", "memory_set_then_search"]
        smoke_runs_dir = EVAL_RUNS_DIR / "smoke"
        smoke_runs_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [str(binary), "--runs", "1", "--run-type", "smoke", "--agent-id", "coding-agent", *sum((["--case", case] for case in smoke_cases), []), "--runs-dir", str(smoke_runs_dir), "--jwt-file", str(jwt_path), "--auth-url", "http://localhost:8090"],
            cwd=str(MCP_ROOT / "platform" / "eval-harness"),
            stdout=eval_log.open("a"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "EVAL_JWT_FILE": str(jwt_path), "EVAL_AGENT_URL": "http://127.0.0.1:8580", "EVAL_AUTH_URL": "http://127.0.0.1:8090"},
            start_new_session=True,
        )
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    _EVAL_PROCESS = proc
    return {"ok": True, "running": True, "pid": proc.pid, "cases": len(smoke_cases), "log": str(eval_log), "instruction": "Quick smoke eval is running; results will refresh automatically."}


@app.post("/api/probe/chat")
async def api_probe_chat(request: Request):
    """One-click: send a chat completion using the cached JWT."""
    body = await request.json()
    prompt = body.get("prompt", "Say hi in 5 words.")
    probe_kind = body.get("probe_kind")
    if probe_kind in {"tool", "fleet"}:
        _write_probe_state(probe_kind, {"status": "running", "prompt": prompt})
    model = body.get("model", "qwen2.5:7b")
    jwt_path = MCP_RUN_DIR / "jwt.txt"
    if not jwt_path.exists():
        return JSONResponse({"ok": False, "error": "no JWT cached — click 'Login' first"}, status_code=400)
    jwt = jwt_path.read_text().strip()
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "http://localhost:8580/v1/chat/completions",
                headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as e:
        if probe_kind in {"tool", "fleet"}:
            _write_probe_state(probe_kind, {"status": "failed", "prompt": prompt, "error": str(e)})
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    result = {
        "ok": r.status_code == 200,
        "http": r.status_code,
        "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:2000],
        "curl": (
            f"curl -sS -X POST http://localhost:8580/v1/chat/completions "
            f"-H 'Authorization: Bearer $(cat {MCP_RUN_DIR}/jwt.txt)' "
            f"-H 'Content-Type: application/json' "
            f"-d '{json.dumps(payload)}'"
        ),
    }
    if probe_kind in {"tool", "fleet"}:
        answer = result["body"].get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(result["body"], dict) else ""
        _write_probe_state(probe_kind, {
            "status": "completed" if result["ok"] else "failed",
            "prompt": prompt, "http": r.status_code, "answer": str(answer)[:1000],
        })
    return result


@app.get("/api/probes")
async def api_probes():
    """Return durable tool/fleet probe states so page reloads preserve them."""
    return {"probes": _read_probe_state()}


# ── UI (single page, no build step) ──────────────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MCP Dev Console</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f1117;
    color: #d7dde6;
    font-size: 13px;
  }
  header {
    padding: 14px 22px;
    border-bottom: 1px solid #232733;
    display: flex; align-items: center; justify-content: space-between;
    background: #161922;
  }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: .3px; }
  header .meta { font-size: 11px; color: #7b8493; }
  .scope-select { background:#0a0c12; border:1px solid #2e3b56; color:#d7dde6; border-radius:5px; padding:5px 8px; font:inherit; font-size:11px; }
  .scope-badge { display:inline-block; margin-left:6px; padding:2px 6px; border:1px solid #2e3b56; border-radius:8px; color:#6cb6ff; font-size:10px; }
  .workspace-nav { display:flex; gap:4px; padding:8px 22px; border-bottom:1px solid #232733; background:#11141c; }
  .workspace-nav button { background:transparent; border-color:transparent; color:#8a93a3; }
  .workspace-nav button.active { background:#243049; border-color:#2e3b56; color:#fff; }
  body[data-view="analysis"] .view-logs { display:none; }
  body[data-view="logs"] .view-analysis { display:none; }
  body[data-view="analysis"] .grid { grid-template-columns: 1fr; }
  body[data-view="analysis"] .grid > .col:nth-child(-n+2) { display:none; }

  .grid {
    display: grid;
    grid-template-columns: 280px 1fr 360px;
    gap: 0;
    height: calc(100vh - 50px);
  }
  .col { overflow: auto; border-right: 1px solid #232733; }
  .col:last-child { border-right: none; }

  .panel { padding: 14px 18px; border-bottom: 1px solid #232733; }
  .panel h2 {
    margin: 0 0 10px 0;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8a93a3;
    font-weight: 600;
  }

  .component {
    display: flex; align-items: center; padding: 8px 6px;
    border-radius: 6px; cursor: pointer; gap: 8px;
    transition: background .1s;
  }
  .component:hover { background: #1d2230; }
  .component.active { background: #243049; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .dot.up   { background: #4cd789; box-shadow: 0 0 8px rgba(76,215,137,.5); }
  .dot.down { background: #ec5b5b; box-shadow: 0 0 6px rgba(236,91,91,.5); }
  .dot.warn { background: #e9b04c; box-shadow: 0 0 6px rgba(233,176,76,.5); }
  /* "not in current scenario" — neutral, unobtrusive */
  .dot.idle { background: #3a4252; border: 1px solid #4a5468; box-shadow: none; }
  .component.idle .name { color: #6c7585; font-weight: 500; }
  .component.idle .port { color: #4a5468; }
  .component .name { font-weight: 600; }
  .component .port { margin-left: auto; color: #6c7585; font-size: 11px; font-variant-numeric: tabular-nums; }

  .metric {
    display: flex; justify-content: space-between; padding: 5px 0;
    border-bottom: 1px dotted #232733;
    font-variant-numeric: tabular-nums;
  }
  .metric:last-child { border-bottom: none; }
  .metric .v { color: #ffffff; font-weight: 600; }

  /* Context-window pressure bar */
  .bar { background:#1d2230; border-radius:3px; height:6px; overflow:hidden; margin-top:4px; }
  .bar > div { height:100%; background:linear-gradient(90deg,#4cd789,#e9b04c,#ec5b5b); }

  /* Per-tool table */
  table.tools { width:100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  table.tools th, table.tools td {
    text-align: left; padding: 4px 6px; border-bottom: 1px solid #1d2230;
    font-size: 11px;
  }
  table.tools th { color:#8a93a3; font-weight:600; }
  table.tools td.num { text-align: right; font-family: "SF Mono", Menlo, monospace; }
  table.tools tr.err td { color: #ec5b5b; }

  /* HTTP breakdown chips */
  .chips { display:flex; gap:4px; flex-wrap:wrap; margin-top:6px; }
  .chip { padding:2px 6px; border-radius:8px; font-size:10px; font-variant-numeric: tabular-nums; }
  .chip.ok  { background:#1b3a26; color:#4cd789; }
  .chip.c4  { background:#3a2d10; color:#e9b04c; }
  .chip.c5  { background:#3a1717; color:#ec5b5b; }
  .chip.dn  { background:#2a2a2a; color:#7b8493; }

  /* Eval pills — 3-color pass-rate badges for the eval panel */
  .evalpill {
    padding: 2px 7px; border-radius: 8px; font-size: 10px;
    font-weight: 600; font-variant-numeric: tabular-nums;
  }
  .evalpill.ok   { background:#1b3a26; color:#4cd789; }
  .evalpill.warn { background:#3a2d10; color:#e9b04c; }
  .evalpill.err  { background:#3a1717; color:#ec5b5b; }
  .donut { width:76px; height:76px; border-radius:50%; display:grid; place-items:center; flex-shrink:0; }
  .donut { transition: transform .16s ease, filter .16s ease; cursor: default; }
  .donut:hover { transform: scale(1.1); filter: drop-shadow(0 0 10px rgba(108,182,255,.35)); }
  .donut::after { content:""; width:48px; height:48px; border-radius:50%; background:#161922; transition: background .16s ease; }
  .donut:hover::after { background:#1d2230; }
  .trend-bar { display:inline-flex; align-items:flex-end; height:34px; margin-right:5px; cursor:help; }
  .trend-bar > span { display:block; width:18px; min-height:3px; border-radius:2px 2px 0 0; transition: width .14s ease, filter .14s ease, opacity .14s ease; }
  .trend-bar:hover > span { width:24px; filter:brightness(1.3) drop-shadow(0 0 5px rgba(108,182,255,.45)); }
  .metric { transition: background .12s ease, padding-left .12s ease, padding-right .12s ease; }
  .metric:hover { background:#1a1f2b; padding-left:6px; padding-right:6px; border-radius:3px; }

  /* Fleet panel (P1 #4) — compact card list + indented timeline detail.
     Indent is 14px per depth level — narrow enough that depth=3 still fits
     in the 360px right column, wide enough to see the staircase shape. */
  .fleet-card {
    padding: 6px 8px; border-radius: 5px;
    background: #161922; border: 1px solid #232733;
    margin-bottom: 6px; cursor: pointer;
    transition: background .1s;
    font-variant-numeric: tabular-nums;
  }
  .fleet-card:hover { background: #1d2230; }
  .fleet-card.active { background: #243049; border-color: #2e3b56; }
  .fleet-card .root {
    font-family: "SF Mono", Menlo, monospace; font-size: 11px;
    color: #c9a4ff; word-break: break-all;
  }
  .fleet-card .agg {
    display: flex; gap: 8px; margin-top: 4px;
    font-size: 10px; color: #8a93a3;
  }
  .fleet-card .agg .err { color: #ec5b5b; font-weight: 600; }
  .fleet-card .agg .dchip {
    background: #1d2230; color: #6cb6ff; padding: 1px 5px;
    border-radius: 7px; font-size: 9px;
  }
  /* Timeline row inside the detail view. Each row indents by depth × 14px,
     vertical-rule "tree-spine" makes parent/child relationship feel real
     without needing actual edges. */
  .fleet-row {
    display: grid;
    grid-template-columns: 50px 1fr 60px 30px;
    gap: 6px; padding: 3px 4px;
    font-family: "SF Mono", Menlo, monospace; font-size: 10px;
    border-bottom: 1px dotted #1d2230;
    line-height: 1.4;
  }
  .fleet-row .t { color: #6c7585; }
  .fleet-row .indent { display: flex; align-items: center; }
  .fleet-row .spine {
    border-left: 2px solid #2e3b56;
    margin-right: 4px;
    align-self: stretch;
  }
  .fleet-row .tool { color: #d7dde6; }
  .fleet-row .comp { color: #c9a4ff; }
  .fleet-row .ms { text-align: right; color: #8a93a3; }
  .fleet-row .st.OK { color: #4cd789; }
  .fleet-row .st.FAIL { color: #ec5b5b; font-weight: 700; }

  .timeline {
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px;
    padding: 0;
  }
  .ev {
    display: grid;
    grid-template-columns: 60px 80px 60px 1fr;
    gap: 8px;
    padding: 4px 18px;
    border-bottom: 1px solid #161922;
    line-height: 1.45;
  }
  .ev .t { color: #6c7585; }
  .ev .c { color: #c9a4ff; }
  .ev .l { font-weight: 700; }
  .ev .l.ERROR { color: #ec5b5b; }
  .ev .l.WARN  { color: #e9b04c; }
  .ev .l.INFO  { color: #6cb6ff; }
  .ev .l.DEBUG { color: #6c7585; }
  .ev .msg { color: #d7dde6; white-space: pre-wrap; word-break: break-all; }

  .log {
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 11px;
    padding: 12px 16px;
    white-space: pre-wrap;
    word-break: break-all;
    color: #b6bcc8;
    line-height: 1.5;
  }
  .log .ERROR { color: #ec5b5b; }
  .log .WARN  { color: #e9b04c; }

  button {
    background: #243049;
    border: 1px solid #2e3b56;
    color: #d7dde6;
    padding: 6px 12px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
  }
  button:hover { background: #2e3b56; }
  button:disabled { opacity: .5; cursor: not-allowed; }

  .probe-result {
    margin-top: 10px;
    padding: 10px;
    background: #0a0c12;
    border-radius: 6px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: 11px;
    max-height: 280px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .probe-result .curl { color: #6cb6ff; margin-bottom: 8px; }
  .probe-result .body { color: #b6bcc8; }

  input[type=text] {
    width: 100%;
    background: #0a0c12;
    border: 1px solid #2e3b56;
    color: #d7dde6;
    padding: 6px 10px;
    border-radius: 5px;
    font-size: 12px;
    font-family: inherit;
  }
</style>
</head>
<body>
<header>
  <h1>🛰  MCP Dev Console</h1>
  <div style="display:flex;align-items:center;gap:8px"><label style="font-size:10px;color:#6c7585">AGENTS</label><span class="scope-badge">Coding</span><span class="scope-badge">Customer QA</span><span class="scope-badge">Ops</span><div class="meta" id="meta">connecting…</div></div>
</header>
<nav class="workspace-nav" aria-label="Dashboard workspace">
  <button id="analysisTab" class="active" onclick="setView('analysis')">Analysis</button>
  <button id="logsTab" onclick="setView('logs')">Logs & Operations</button>
</nav>
<div class="grid">

  <div class="col">
    <div class="panel view-logs">
      <h2>Components</h2>
      <div id="components"></div>
    </div>
    <div class="panel view-logs">
      <h2>Quick probes</h2>
      <div style="display: flex; flex-direction: column; gap: 8px;">
        <button onclick="probeLogin()">① Login (signup if needed)</button>
        <input type="text" id="chatPrompt" placeholder="prompt for chat probe" value="Say hi in 5 words.">
        <button onclick="probeChat()">② Send chat</button>
        <button onclick="probeMemorySet()">③ memory_set + search</button>
        <button onclick="probeCodeRun()">④ code_run (python)</button>
      </div>
      <div id="probeResult" class="probe-result" style="display:none"></div>
    </div>
  </div>

  <div class="col">
    <div class="panel view-logs">
      <h2>Activity (live)</h2>
      <div id="timeline" class="timeline"></div>
    </div>
  </div>

  <div class="col">
    <div class="panel view-analysis">
      <h2>Agent metrics</h2>
      <div id="agentMetrics"></div>
    </div>
    <div class="panel view-analysis">
      <h2>Feedback loop <span class="scope-badge">agent field preserved</span></h2>
      <div id="feedbackMetrics">loading feedback state...</div>
    </div>
    <div class="panel view-analysis">
      <h2>Tool stats (per backend) <span class="scope-badge">All agents</span></h2>
      <div id="toolStats">no data yet</div>
    </div>
    <div class="panel view-analysis">
      <h2>Agent fleets <span class="scope-badge">coding-agent probe</span><span id="fleetCountBadge" style="font-size:10px;color:#888;font-weight:normal"></span></h2>
      <div id="fleetList" style="font-size:11px;color:#888">no fleets yet — start a multi-agent chat (e.g. <code>spawn_subagent</code>)</div>
      <div id="fleetDetail" style="margin-top:10px;display:none"></div>
    </div>
    <div class="panel view-analysis">
      <h2>Eval harness <span class="scope-badge">coding-agent</span><span id="evalAgeBadge" style="font-size:10px;color:#888;font-weight:normal"></span></h2>
      <div id="evalResults">no eval runs yet — try <code>eval-run</code></div>
    </div>
    <div class="panel view-logs">
      <h2><span id="logTitle">Log — agent</span></h2>
      <div style="display:flex; gap:8px; margin-bottom:8px;">
        <button onclick="refreshLog()">Refresh</button>
        <button onclick="setAutoRefresh()">Auto-refresh: <span id="autoOnOff">off</span></button>
      </div>
      <div id="log" class="log">select a component</div>
    </div>
  </div>

</div>

<script>
let activeComponent = "agent";
document.body.dataset.view = "analysis";
let autoRefresh = false;
let autoRefreshTimer = null;
let evalRunning = false;
const analysisProbeState = { tool: false, fleet: false };
const analysisProbeResults = { tool: "", fleet: "" };
// Currently-selected fleet (root_session_id) for the detail view.
// null = no detail panel shown; the user must click a fleet card to drill in.
let selectedFleet = null;

function setView(view) {
  document.body.dataset.view = view;
  document.getElementById("analysisTab").classList.toggle("active", view === "analysis");
  document.getElementById("logsTab").classList.toggle("active", view === "logs");
}

async function fetchProbes() {
  try {
    const response = await fetch("/api/probes");
    const body = await response.json();
    for (const kind of ["tool", "fleet"]) {
      const probe = body.probes?.[kind];
      if (!probe) continue;
      analysisProbeState[kind] = probe.status === "running";
      analysisProbeResults[kind] = probe.status === "completed"
        ? `Last result: ${String(probe.answer || "request completed; no answer text returned").replace(/\s+/g, " ").slice(0, 180)}`
        : probe.status === "failed" ? `Probe failed: ${probe.error || "unknown error"}` : "";
    }
  } catch (e) { /* probe state is best-effort */ }
}
fetchProbes();


async function fetchStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();
    renderComponents(d.components, d.probe_breakdown);
    renderAgentMetrics(d.metrics.agent || {}, d.active_sessions);
    renderToolStats(d.metrics);
    fetchProbes().then(() => { renderToolStats(d.metrics); fetchFleets(); });
    fetchFeedback();
    const scen = d.scenario ? `scenario: ${d.scenario}` : "scenario: (none registered)";
    document.getElementById("meta").textContent =
      `${scen}  •  run_dir: ${d.run_dir}  •  sessions(10m): ${d.active_sessions}  •  ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    document.getElementById("meta").textContent = "dashboard offline?";
  }
  // Eval results poll on the same cadence — they're tiny static files,
  // re-reading every 5s is free and keeps the UI in sync after eval-run.
  fetchEval();
  fetchFleets();
}

async function fetchFeedback() {
  try {
    const r = await fetch("/api/feedback");
    renderFeedback(await r.json());
  } catch (e) {
    document.getElementById("feedbackMetrics").textContent = "feedback state unavailable";
  }
}

function renderFeedback(d) {
  const implicit = d.implicit || {};
  const explicit = d.explicit || {};
  const reranker = d.reranker || {};
  const retrieval = d.retrieval || {};
  const quality = d.quality || {};
  const labels = implicit.labels || {};
  const reasons = explicit.reasons || {};
  const state = reranker.ready_to_retrain ? "ready for retrain" :
    (implicit.samples ? "collecting samples" : "no implicit samples yet");
  const labelRows = Object.entries(labels).map(([k, v]) =>
    `<div class="metric"><span>implicit label ${escapeHtml(k)}</span><span class="v">${v}</span></div>`).join("");
  const reasonRows = Object.entries(reasons).map(([k, v]) =>
    `<div class="metric"><span>${escapeHtml(k)}</span><span class="v">${v}</span></div>`).join("");
  const weightNames = ["name", "exact", "terms", "function", "class", "brevity", "retrieval"];
  const weights = Array.isArray(reranker.weights) ? reranker.weights : [];
  const weightRows = weights.length ? `<div style="margin-top:7px;color:#888;font-size:10px">current weights</div>` +
    weights.map((v, i) => `<div class="metric"><span>${weightNames[i] || "feature"}</span><span class="v">${Number(v).toFixed(3)}</span></div>`).join("") : "";
  document.getElementById("feedbackMetrics").innerHTML = `
    <div class="metric"><span>Implicit samples</span><span class="v">${implicit.samples || 0}</span></div>
    <div class="metric"><span>Explicit ratings</span><span class="v">${explicit.samples || 0}</span></div>
    <div class="metric"><span>Positive rating rate</span><span class="v">${explicit.positive_rate == null ? "—" : `${(explicit.positive_rate * 100).toFixed(1)}%`}</span></div>
    <div class="metric"><span>Retrieval traces</span><span class="v">${retrieval.traces || 0}</span></div>
    <div class="metric"><span>Feedback linked to trace</span><span class="v">${retrieval.linked_feedback || 0}</span></div>
    <div class="metric"><span>Trace link rate</span><span class="v">${retrieval.link_rate == null ? "—" : `${(retrieval.link_rate * 100).toFixed(1)}%`}</span></div>
    <div class="metric"><span>Average chunks / trace</span><span class="v">${retrieval.avg_chunks == null ? "—" : retrieval.avg_chunks}</span></div>
    <div class="metric"><span>QA Recall@K</span><span class="v">${quality.available ? quality.recall_at_k : "—"}</span></div>
    <div class="metric"><span>QA MRR@K</span><span class="v">${quality.available ? quality.mrr_at_k : "—"}</span></div>
    <div class="metric"><span>Hard-negative @K</span><span class="v">${quality.available ? quality.hard_negative_leak_rate : "—"}</span></div>
    <div class="metric"><span>Iteration state</span><span class="v">${state}</span></div>
    ${labelRows || `<div style="color:#888;font-size:11px">No implicit retrieval labels yet</div>`}
    ${reasonRows ? `<div style="margin-top:7px;color:#888;font-size:10px">user feedback reasons</div>${reasonRows}` : ""}
    ${weightRows}
    <div style="margin-top:8px;color:#666;font-size:10px;word-break:break-all">persisted: ${escapeHtml(implicit.path || "—")}</div>`;
}

async function fetchEval() {
  try {
    const r = await fetch("/api/eval");
    const d = await r.json();
    renderEval(d);
  } catch (e) {
    // Silent — eval is optional; missing files are normal in dev.
  }
}

async function fetchFleets() {
  // Poll the fleet list and re-render. If the user has a fleet selected
  // (`selectedFleet`), also re-fetch its detail so the indented timeline
  // stays live during a long-running multi-agent request.
  try {
    const r = await fetch("/api/fleets");
    const d = await r.json();
    renderFleetList(d.fleets || []);
    if (selectedFleet) {
      const dr = await fetch(`/api/fleets/${encodeURIComponent(selectedFleet)}`);
      renderFleetDetail(await dr.json());
    }
  } catch (e) {
    // Silent — fleet view is best-effort.
  }
}

function renderComponents(comps, breakdowns) {
  const root = document.getElementById("components");
  root.innerHTML = "";
  for (const c of comps) {
    // Three buckets:
    //   UP                          → green dot
    //   DOWN but EXPECTED           → red dot, "DOWN" label, chips visible
    //   DOWN and NOT EXPECTED       → grey dot, "not in scenario" label, no alarms
    //   Some HTTP code other than 0/200 → yellow dot
    let cls;
    let displayState = c.state;
    let idle = false;
    if (c.code === 200) {
      cls = "up";
    } else if (c.code === 0 && !c.expected) {
      cls = "idle";
      idle = true;
      displayState = "not in scenario";
    } else if (c.code === 0) {
      cls = "down";
    } else {
      cls = "warn";
    }
    const div = document.createElement("div");
    div.className = "component"
      + (c.name === activeComponent ? " active" : "")
      + (idle ? " idle" : "");
    div.onclick = () => { activeComponent = c.name; refreshLog(); fetchStatus(); };
    const b = (breakdowns || {})[c.name] || {};
    // chips show the last ~5 min of probe outcomes — instantly surfaces
    // intermittent failures that disappear behind a current "UP" status.
    // Suppress them entirely for idle components (no signal, just noise).
    const chips = (!idle && b.total) ? `
      <div class="chips">
        ${b.ok ? `<span class="chip ok">${b.ok}× 2xx</span>` : ""}
        ${b.client_err ? `<span class="chip c4">${b.client_err}× 4xx</span>` : ""}
        ${b.server_err ? `<span class="chip c5">${b.server_err}× 5xx</span>` : ""}
        ${b.down ? `<span class="chip dn">${b.down}× down</span>` : ""}
        <span class="chip dn">${b.avg_ms}ms avg</span>
      </div>` : "";
    div.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;width:100%">
        <span class="dot ${cls}"></span>
        <span class="name">${c.name}</span>
        <span class="port">:${c.port} (${displayState})</span>
      </div>
      ${chips}`;
    root.appendChild(div);
  }
}

// Rough heuristic for context-window pressure. We don't actually know
// the model's true context size, so we use 32K as a conservative anchor
// for "small local model" — adjust if you swap to gpt-4o/claude.
const CONTEXT_BUDGET = 32 * 1024;

function renderAgentMetrics(m, activeSessions) {
  const root = document.getElementById("agentMetrics");
  const ctxPct = Math.min(100, Math.round(((m.max_prompt_tokens || 0) / CONTEXT_BUDGET) * 100));
  const cost = (m.cost_usd || 0).toFixed(4);
  const rows = [
    ["Requests (Σ)", m.requests || 0],
    ["Active sessions (10m)", activeSessions || 0],
    ["Tool calls (Σ)", m.tool_calls || 0],
    ["Prompt tokens (Σ)", (m.prompt_tokens || 0).toLocaleString()],
    ["Completion tokens (Σ)", (m.completion_tokens || 0).toLocaleString()],
    ["Cost (Σ)", `$${cost}`],
    ["Latency p50", (m.p50_ms ? m.p50_ms + " ms" : "—")],
    ["Latency p95", (m.p95_ms ? m.p95_ms + " ms" : "—")],
    ["Latency p99", (m.p99_ms ? m.p99_ms + " ms" : "—")],
    ["Errors (5m)", m.errors_5m || 0],
  ];
  root.innerHTML = rows.map(([k,v]) =>
    `<div class="metric"><span>${k}</span><span class="v">${v}</span></div>`
  ).join("") + `
    <div class="metric">
      <span>Context peak</span>
      <span class="v">${(m.max_prompt_tokens || 0).toLocaleString()} / ${CONTEXT_BUDGET.toLocaleString()} (${ctxPct}%)</span>
    </div>
    <div class="bar"><div style="width:${ctxPct}%"></div></div>`;
  if (m.last_error) {
    root.innerHTML += `
      <div class="metric" style="border-top:1px solid #232733;padding-top:8px;margin-top:6px">
        <span style="color:#ec5b5b">last error</span></div>
      <div style="font-family:monospace;font-size:10px;color:#ec5b5b;white-space:pre-wrap;word-break:break-all">${escapeHtml(m.last_error)}</div>`;
  }
}

function renderToolStats(metrics) {
  // Aggregate per-tool rows across all backends. Each row shows:
  // backend, tool, calls, errors, p95, last latency.
  const rows = [];
  for (const [comp, m] of Object.entries(metrics || {})) {
    const tools = m.tools || {};
    for (const [name, t] of Object.entries(tools)) {
      rows.push({comp, name, ...t});
    }
  }
  const root = document.getElementById("toolStats");
  if (!rows.length) {
    root.innerHTML = `<div style="color:#888;font-size:11px">No tool calls observed yet.</div>
      <div style="margin-top:8px;color:#6c7585;font-size:10px;line-height:1.5">
        Trigger examples:<br><code>Read README.md and summarize it</code> → <code>file_read</code><br>
        <code>Remember my preferred language is Chinese</code> → memory tool<br>
        <code>Run python to calculate 2 + 2</code> → <code>code_run</code>
      </div>`;
    root.insertAdjacentHTML("beforeend", `<button onclick="runAnalysisProbe('tool')" style="margin-top:9px" ${analysisProbeState.tool ? "disabled" : ""}>${analysisProbeState.tool ? "Tool probe running…" : "Run tool smoke"}</button><div id="toolProbeNotice" style="margin-top:6px;color:#6c7585;font-size:10px">${analysisProbeState.tool ? "Waiting for tool telemetry…" : analysisProbeResults.tool}</div>`);
    return;
  }
  rows.sort((a,b) => b.calls - a.calls);
  root.innerHTML = `
    <table class="tools">
      <thead><tr>
        <th>backend</th><th>tool</th>
        <th class="num">calls</th><th class="num">err</th>
        <th class="num">p95</th><th class="num">last</th>
      </tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr class="${r.errors ? "err" : ""}">
            <td>${escapeHtml(r.comp)}</td>
            <td>${escapeHtml(r.name)}</td>
            <td class="num">${r.calls}</td>
            <td class="num">${r.errors}</td>
            <td class="num">${r.p95_ms || "—"}</td>
            <td class="num">${r.last_ms || "—"}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

// ── Eval results panel ───────────────────────────────────────────────
// The harness writes runs/latest.json and runs/latest-bfcl.json. We
// just present them as two compact summary cards + a per-scorer
// breakdown. Click a case row to see the failing scorer's detail.
function renderEval(d) {
  const root = document.getElementById("evalResults");
  const openFailures = new Set([...root.querySelectorAll("details[data-failure-id][open]")].map((node) => node.dataset.failureId));
  // Do not replace the eval subtree while an analyst is reading a failure.
  // The next scheduled refresh will apply as soon as all details are closed.
  if (openFailures.size > 0) return;
  const badge = document.getElementById("evalAgeBadge");
  const haveAgent = d.agent && !d.agent.error;
  const haveSmoke = d.smoke && !d.smoke.error;
  const haveBfcl = d.bfcl && !d.bfcl.error;
  if (!haveAgent && !haveSmoke && !haveBfcl) {
    root.innerHTML = `<div style="color:#888;font-size:11px">
      No eval runs yet. From <code>${escapeHtml(d.runs_dir || "")}</code>:
      <br>• <code>eval-run</code> — full agent loop
      <br>• <code>eval-suite-bfcl</code> — bare-LLM tool-call benchmark
      <br><button onclick="runEval()" style="margin-top:8px">Run smoke eval</button>
    </div>`;
    badge.textContent = "";
    return;
  }
  // Show the freshest mtime in the badge so you can see "is this stale?"
  const newest = Math.max(haveAgent ? d.agent._mtime : 0, haveSmoke ? d.smoke._mtime : 0, haveBfcl ? d.bfcl._mtime : 0);
  badge.textContent = " · last run " + relativeTime(newest);

  let html = "";
  if (evalRunning) html += `<div id="evalRunNotice" style="color:#e9b04c;font-size:11px;margin-bottom:8px">Smoke eval is running… results will refresh automatically.</div>`;
  const useSmokeSummary = haveSmoke && (!d.agent || d.smoke._mtime >= d.agent._mtime);
  const failures = useSmokeSummary ? (d.smoke_failures || {}) : (d.agent_failures || {});
  const failureRows = Object.entries(failures).sort((a, b) => b[1] - a[1]).map(([name, count]) =>
    `<div class="metric"><span>${escapeHtml(name)}</span><span class="v" style="color:#ec5b5b">${count} failed</span></div>`).join("");
  if (failureRows) {
    html += `<div style="margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #232733"><div style="color:#8a93a3;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:5px">Failure distribution · latest ${useSmokeSummary ? "smoke" : "full"} run</div>${failureRows}</div>`;
  }
  const history = Array.isArray(d.history) ? d.history.filter((r) => r.suite === "agent") : [];
  if (history.length > 1) {
    const trend = history.slice(0, 8).reverse();
    const points = trend.map((r) => {
      const pct = Math.round((r.pass_rate || 0) * 100);
      return `<span class="trend-bar" title="${escapeHtml(r.ts || "unknown run")} · ${pct}% passed"><span style="height:${Math.max(3, pct)}%;background:${pct >= 90 ? "#4cd789" : pct >= 70 ? "#e9b04c" : "#ec5b5b"}"></span></span>`;
    }).join("");
    const latest = history[0].pass_rate == null ? "—" : `${Math.round(history[0].pass_rate * 100)}%`;
    const previous = history[1].pass_rate == null ? null : Math.round(history[1].pass_rate * 100);
    const delta = previous == null || history[0].pass_rate == null ? "" : ` · ${Math.round(history[0].pass_rate * 100) - previous >= 0 ? "+" : ""}${Math.round(history[0].pass_rate * 100) - previous}pp vs previous`;
    html += `<div style="margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid #232733"><div class="metric"><span>Agent quality trend</span><span class="v">${latest}${delta}</span></div><div style="height:38px;display:flex;align-items:flex-end;padding-top:4px">${points}</div><div style="color:#6c7585;font-size:10px">last ${history.length} runs · hover bars for timestamp</div></div>`;
  }
  if (haveSmoke) html += renderEvalSection("Smoke eval · 3 cases · dashboard button", d.smoke, /*showScorers*/true);
  if (haveAgent) html += renderEvalSection("Full Agent suite · command line", d.agent, /*showScorers*/true);
  if (haveBfcl) html += renderEvalSection("BFCL (model tool-calling)", d.bfcl, /*showScorers*/false);
  html += `<div style="margin-top:8px;color:#6c7585;font-size:10px;line-height:1.5">
    Agent suite: end-to-end prompt → planner → tools → answer assertions.<br>
    BFCL: model tool-call selection without the full agent loop.<br>
    <button onclick="runEval()" style="margin-top:7px">Run smoke eval</button>
  </div>`;
  root.innerHTML = html;
  for (const node of root.querySelectorAll("details[data-failure-id]")) {
    node.open = openFailures.has(node.dataset.failureId);
  }
}

async function runEval() {
  const root = document.getElementById("evalResults");
  evalRunning = true;
  root.insertAdjacentHTML("afterbegin", `<div id="evalRunNotice" style="color:#e9b04c;font-size:11px;margin-bottom:8px">Starting smoke eval…</div>`);
  document.querySelectorAll('button[onclick="runEval()"]')?.forEach((button) => {
    button.disabled = true;
    button.textContent = "Eval running…";
  });
  try {
    const r = await fetch("/api/probe/eval", { method: "POST" });
    const d = await r.json();
    const note = d.ok ? `Smoke eval running (pid ${d.pid}). This exercises ${d.cases || "the configured"} cases; results will refresh automatically.` : `eval unavailable: ${d.error}`;
    const notice = document.getElementById("evalRunNotice");
    if (notice) { notice.textContent = note; notice.style.color = d.ok ? "#4cd789" : "#ec5b5b"; }
    if (d.ok) setTimeout(() => {
      evalRunning = false;
      document.querySelectorAll('button[onclick="runEval()"]')?.forEach((button) => { button.disabled = false; button.textContent = "Run smoke eval"; });
      fetchEval();
    }, 180000);
    if (!d.ok) { evalRunning = false; document.querySelectorAll('button[onclick="runEval()"]')?.forEach((button) => { button.disabled = false; button.textContent = "Run smoke eval"; }); }
  } catch (e) {
    const notice = document.getElementById("evalRunNotice");
    if (notice) { notice.textContent = "Could not start eval"; notice.style.color = "#ec5b5b"; }
    evalRunning = false;
    document.querySelectorAll('button[onclick="runEval()"]')?.forEach((button) => { button.disabled = false; button.textContent = "Run smoke eval"; });
  }
}

async function runAnalysisProbe(kind) {
  const isFleet = kind === "fleet";
  analysisProbeState[kind] = true;
  const notice = document.getElementById(isFleet ? "fleetProbeNotice" : "toolProbeNotice");
  const prompt = isFleet
    ? "Inspect README.md, pom.xml, and Dockerfile in parallel and summarize the differences."
    : "Run python to calculate 2 + 2 and report the result.";
  if (notice) notice.textContent = "starting probe…";
  try {
    const login = await fetch("/api/probe/login", {method: "POST"});
    if (!login.ok) throw new Error("default probe identity unavailable");
    const response = await fetch("/api/probe/chat", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({prompt, probe_kind: kind})
    });
    const body = await response.json();
    if (notice) {
      const answer = body.body?.choices?.[0]?.message?.content || body.body?.error || "";
      const preview = String(answer).replace(/\s+/g, " ").slice(0, 180);
      analysisProbeResults[kind] = body.ok
        ? `Last result: ${preview || "request completed; no answer text returned"}`
        : `Probe failed: ${body.error || "unknown error"}`;
      notice.textContent = body.ok
        ? `probe completed · ${isFleet ? "fleet" : "tool"} telemetry is being refreshed${preview ? ` · ${preview}` : ""}`
        : `probe failed: ${body.error || "unknown error"}`;
      notice.style.color = body.ok ? "#4cd789" : "#ec5b5b";
    }
    if (!body.ok) analysisProbeState[kind] = false;
    const started = Date.now();
    const poll = setInterval(async () => {
      await fetchStatus();
      await fetchFleets();
      if (Date.now() - started > 60000) {
        clearInterval(poll);
        analysisProbeState[kind] = false;
        fetchStatus();
      }
    }, 2000);
  } catch (error) {
    analysisProbeState[kind] = false;
    if (notice) { notice.textContent = "probe request failed"; notice.style.color = "#ec5b5b"; }
  }
}

function renderEvalSection(title, s, showScorers) {
  // Top-line: X/Y passed with a 3-state pill (green ≥90%, yellow ≥70%, red below).
  const pct = Math.round((s.pass_rate || 0) * 100);
  const cls = pct >= 90 ? "ok" : pct >= 70 ? "warn" : "err";

  // Per-scorer rollup is the most useful "where am I failing?" view.
  // Only the agent suite has by_scorer (BFCL uses by_category).
  let scorersHtml = "";
  if (showScorers && s.by_scorer) {
    const rows = Object.entries(s.by_scorer).map(([name, b]) => {
      const p = b.total ? Math.round(b.passed / b.total * 100) : 0;
      const cc = p === 100 ? "ok" : p >= 70 ? "warn" : "err";
      return `<tr>
        <td>${escapeHtml(name)}</td>
        <td class="num">${b.passed}/${b.total}</td>
        <td class="num"><span class="evalpill ${cc}">${p}%</span></td>
      </tr>`;
    }).join("");
    scorersHtml = `<table class="tools" style="margin-top:6px"><thead><tr>
      <th>scorer</th><th class="num">pass</th><th class="num">rate</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  } else if (!showScorers && s.by_category) {
    const rows = Object.entries(s.by_category).map(([cat, b]) => {
      const p = b.total ? Math.round(b.passed / b.total * 100) : 0;
      const cc = p === 100 ? "ok" : p >= 70 ? "warn" : "err";
      return `<tr><td>${escapeHtml(cat)}</td>
        <td class="num">${b.passed}/${b.total}</td>
        <td class="num"><span class="evalpill ${cc}">${p}%</span></td></tr>`;
    }).join("");
    scorersHtml = `<table class="tools" style="margin-top:6px"><thead><tr>
      <th>category</th><th class="num">pass</th><th class="num">rate</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  }

  // Failing cases get a one-line summary with the FIRST failing scorer.
  const fails = (s.cases || []).filter(c => !c.passed);
  let failsHtml = "";
  if (fails.length) {
    failsHtml = `<div style="margin-top:8px;font-size:11px;color:#ec5b5b">
      ${fails.length} failing:
      <ul style="margin:4px 0 0 16px;padding:0">
        ${fails.slice(0,6).map(c => {
          const firstFail = (c.scores || []).find(s => !s.passed);
          const why = firstFail ? `${firstFail.name}: ${firstFail.detail}` : (c.detail || "");
          return `<li>
            <strong>${escapeHtml(c.case_id)}</strong>
            <details data-failure-id="${escapeAttr(c.case_id)}" style="display:inline;color:#888"><summary style="display:inline;cursor:pointer">— show failure details</summary><div style="margin:4px 0 4px 12px;color:#b6bcc8;white-space:pre-wrap;word-break:break-word">${escapeHtml(why)}</div></details>
          </li>`;
        }).join("")}
      </ul>
    </div>`;
  }

  // Optional latency view (BFCL has it per case, agent has it per case).
  const cases = s.cases || [];
  const lat = cases.map(c => c.latency_ms || 0).filter(x => x > 0);
  const lat_p50 = lat.length ? lat.sort((a,b)=>a-b)[Math.floor(lat.length/2)] : null;

  const passed = Number(s.passed || 0);
  const failed = Math.max(0, Number(s.total || 0) - passed);
  const total = Math.max(1, Number(s.total || 0));
  const successPct = Math.round(passed / total * 100);
  const donut = `conic-gradient(#4cd789 0 ${successPct}%, #ec5b5b ${successPct}% 100%)`;
  return `<div style="margin-bottom:14px">
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:4px">
      <strong>${escapeHtml(title)}</strong>
      <span class="evalpill ${cls}">${s.passed}/${s.total} (${pct}%)</span>
      ${lat_p50 ? `<span style="font-size:10px;color:#888">p50 latency: ${lat_p50}ms</span>` : ""}
    </div>
    <div style="display:flex;gap:12px;align-items:center;margin:8px 0 12px">
      <div class="donut" style="background:${donut}" title="${passed} passed, ${failed} failed"></div>
      <div style="font-size:11px;line-height:1.7"><div><span style="color:#4cd789">●</span> success ${passed}</div><div><span style="color:#ec5b5b">●</span> failed ${failed}</div><div style="color:#6c7585">Each case is an assertion set, not a subjective model thought score.</div></div>
    </div>
    ${scorersHtml}
    ${failsHtml}
  </div>`;
}

// ── Fleet panel (P1 #4) ──────────────────────────────────────────────
// Renders the list of recent agent fleets + (when clicked) an indented
// chronological timeline of every audit row in that fleet. The indent
// is the depth from X-Agent-Depth, so a fan-out request visually steps
// to the right as subagents start, and back to the left when they return
// and the parent picks up the next call.
function renderFleetList(fleets) {
  const root = document.getElementById("fleetList");
  const badge = document.getElementById("fleetCountBadge");
  badge.textContent = fleets.length ? `· ${fleets.length}` : "";
  if (!fleets.length) {
    root.innerHTML = `<div style="color:#888;font-size:11px">no fleets yet</div>
      <div style="margin-top:8px;color:#6c7585;font-size:10px;line-height:1.5">
        Trigger in Coding Agent:<br><code>Inspect these 3 files in parallel and summarize the differences.</code><br>
        The parent request is the root; spawned workers appear as indented rows.
      </div><button onclick="runAnalysisProbe('fleet')" style="margin-top:9px" ${analysisProbeState.fleet ? "disabled" : ""}>${analysisProbeState.fleet ? "Fleet probe running…" : "Run fleet smoke"}</button><div id="fleetProbeNotice" style="margin-top:6px;color:#6c7585;font-size:10px">${analysisProbeState.fleet ? "Waiting for fleet telemetry…" : analysisProbeResults.fleet}</div>`;
    return;
  }
  root.innerHTML = fleets.map(f => {
    const ago = relativeTime(f.last_seen);
    // Depth histogram chips: one chip per depth level seen. Compact,
    // gives an at-a-glance shape ("0:1, 1:5" = root + 5 children).
    const depthChips = Object.entries(f.depth_histogram || {})
      .sort(([a],[b]) => Number(a) - Number(b))
      .map(([d, n]) => `<span class="dchip">d${d}:${n}</span>`)
      .join(" ");
    const errPart = f.errors
      ? `<span class="err">${f.errors} err</span>`
      : "";
    const trunc = f.rows_truncated
      ? `<span class="dchip" title="row buffer full; older rows dropped">…trunc</span>`
      : "";
    const active = f.root_session === selectedFleet ? " active" : "";
    // root_session_id may be long — clip in the card but keep full text
    // in title= for hover. Detail view always shows the full id.
    const rootDisplay = f.root_session.length > 28
      ? f.root_session.slice(0, 25) + "…"
      : f.root_session;
    return `<div class="fleet-card${active}"
                 onclick="selectFleet('${escapeAttr(f.root_session)}')"
                 title="${escapeAttr(f.root_session)}">
      <div class="root">${escapeHtml(rootDisplay)}</div>
      <div class="agg">
        <span>${f.calls} calls</span>
        ${errPart}
        <span>${f.wall_ms}ms</span>
        <span>${ago}</span>
        ${depthChips}
        ${trunc}
      </div>
    </div>`;
  }).join("");
}

function renderFleetDetail(d) {
  const root = document.getElementById("fleetDetail");
  if (!d || !d.root_session) {
    root.style.display = "none";
    return;
  }
  root.style.display = "block";
  if (d.evicted || !d.rows.length) {
    root.innerHTML = `<div style="font-size:11px;color:#888">
      <div><strong>${escapeHtml(d.root_session)}</strong></div>
      <div>no rows (fleet may have been evicted)</div>
      <button onclick="selectFleet(null)" style="margin-top:4px">close</button>
    </div>`;
    return;
  }
  // Header: root_session + aggregates.
  const truncNote = d.rows_truncated
    ? `<span class="dchip" title="row buffer full; oldest rows dropped">…trunc</span>`
    : "";
  // Timeline rows, oldest-first. Each row:
  //   [time] [indent-spines × depth] [tool@component] [duration] [status]
  // Time shown is relative to first_seen (Δms) so the eye reads "spacing
  // between calls" rather than wall-clock — much more useful for spotting
  // a hot loop or a 5s LLM stall.
  const t0 = d.first_seen;
  const rowsHtml = d.rows.map(r => {
    const dt = Math.max(0, Math.round((r.ts - t0) * 1000));
    // depth>0: render N spine bars before the tool name. Each .spine
    // is 14px wide via grid; we just stack them inline.
    let spines = "";
    for (let i = 0; i < r.depth; i++) {
      spines += `<span class="spine" style="width:12px;margin-right:2px"></span>`;
    }
    return `<div class="fleet-row" title="parent=${escapeAttr(r.parent_session)}">
      <span class="t">+${dt}ms</span>
      <span class="indent">${spines}<span class="tool">${escapeHtml(r.tool)}</span>
        <span style="color:#6c7585">@</span>
        <span class="comp">${escapeHtml(r.component)}</span></span>
      <span class="ms">${r.duration_ms}ms</span>
      <span class="st ${r.status}">${r.status === "OK" ? "✓" : "✗"}</span>
    </div>`;
  }).join("");
  root.innerHTML = `
    <div style="font-size:11px;margin-bottom:6px">
      <div style="font-family:monospace;color:#c9a4ff;word-break:break-all">
        ${escapeHtml(d.root_session)}
      </div>
      <div style="color:#8a93a3;margin-top:3px">
        ${d.calls} calls · max depth ${d.max_depth} · ${d.wall_ms}ms wall
        ${d.errors ? `· <span style="color:#ec5b5b">${d.errors} err</span>` : ""}
        ${truncNote}
        <button onclick="selectFleet(null)" style="margin-left:6px;padding:1px 6px;font-size:10px">×</button>
      </div>
    </div>
    <div style="max-height:280px;overflow:auto;border-top:1px solid #232733;padding-top:4px">
      ${rowsHtml}
    </div>`;
}

function selectFleet(rootSessionId) {
  selectedFleet = rootSessionId;
  // Immediately re-fetch so the click is responsive (don't wait for the
  // 3s status poll).
  fetchFleets();
}

// Attribute-safe escape (single quotes blow up our onclick="selectFleet('…')")
function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/'/g, "&#39;")
    .replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function relativeTime(epochSec) {
  if (!epochSec) return "never";
  const ageSec = Math.floor(Date.now() / 1000 - epochSec);
  if (ageSec < 60) return `${ageSec}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec/60)}m ago`;
  if (ageSec < 86400) return `${Math.floor(ageSec/3600)}h ago`;
  return `${Math.floor(ageSec/86400)}d ago`;
}

async function refreshLog() {
  document.getElementById("logTitle").textContent = `Log — ${activeComponent}`;
  try {
    const r = await fetch(`/api/log/${activeComponent}?tail=300`);
    const d = await r.json();
    const root = document.getElementById("log");
    if (!d.lines.length) {
      root.textContent = d.note || "(empty)";
      return;
    }
    root.innerHTML = d.lines.map(l => {
      let cls = "";
      if (/\bERROR\b/.test(l)) cls = "ERROR";
      else if (/\bWARN(ING)?\b/.test(l)) cls = "WARN";
      return `<span class="${cls}">${escapeHtml(l)}</span>`;
    }).join("\n");
    root.scrollTop = root.scrollHeight;
  } catch (e) {
    document.getElementById("log").textContent = "(error: " + e.message + ")";
  }
}

function setAutoRefresh() {
  autoRefresh = !autoRefresh;
  document.getElementById("autoOnOff").textContent = autoRefresh ? "on" : "off";
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  if (autoRefresh) autoRefreshTimer = setInterval(refreshLog, 2000);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function renderEvents(events) {
  const root = document.getElementById("timeline");
  for (const ev of events) {
    const div = document.createElement("div");
    div.className = "ev";
    const ts = new Date(ev.ts * 1000).toLocaleTimeString();
    div.innerHTML = `
      <span class="t">${ts}</span>
      <span class="c">${ev.component}</span>
      <span class="l ${ev.level}">${ev.level}</span>
      <span class="msg">${escapeHtml(ev.text)}</span>`;
    root.insertBefore(div, root.firstChild);
  }
  // Cap rendered nodes to avoid runaway DOM growth.
  while (root.children.length > 400) root.removeChild(root.lastChild);
}

// SSE stream for live events
const es = new EventSource("/api/stream");
es.onmessage = (e) => {
  try {
    const events = JSON.parse(e.data);
    if (events && events.length) renderEvents(events);
  } catch {}
};

// ── Probe buttons ──
async function showResult(data) {
  const el = document.getElementById("probeResult");
  el.style.display = "block";
  let html = "";
  if (data.curl) html += `<div class="curl">$ ${escapeHtml(data.curl)}</div>`;
  html += `<div class="body">${escapeHtml(typeof data === "string" ? data : JSON.stringify(data, null, 2))}</div>`;
  el.innerHTML = html;
}

async function probeLogin() {
  const r = await fetch("/api/probe/login", { method: "POST" });
  showResult(await r.json());
  fetchStatus();
}
async function probeChat() {
  const prompt = document.getElementById("chatPrompt").value;
  const r = await fetch("/api/probe/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  showResult(await r.json());
}
async function probeMemorySet() {
  const r = await fetch("/api/probe/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Use memory_set to save key=favorite_color content=blue, then memory_search for favorite_color, then state the value." }),
  });
  showResult(await r.json());
}
async function probeCodeRun() {
  const r = await fetch("/api/probe/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: "Call code_run with code=print(2+2) language=python timeout=5. Report the output." }),
  });
  showResult(await r.json());
}

// boot
fetchStatus();
refreshLog();
setInterval(fetchStatus, 3000);
</script>
</body>
</html>
"""


@app.get("/")
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=DASHBOARD_PORT, log_level="warning")
