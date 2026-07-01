"""Tests for the dev dashboard's audit-log parsing + fleet tracker (P1 #4).

The dashboard lives at scripts/dev/dashboard/dashboard.py — outside the
agent-server package. We put tests here because:
  1. This is the only Python pytest invocation in the Makefile.
  2. The dashboard imports nothing from agent-server, so the dependency
     direction is one-way and clean.
  3. Adding a separate pytest target just for one file is overkill.

We import the module dynamically by absolute path so we don't pollute
agent-server's import space with a sibling-folder hack.

Coverage:
  - _AUDIT_RE matches BOTH legacy and post-P1#3 audit lines
  - Legacy lines still update per-tool metrics; just no fleet entry
  - Post-P1#3 lines populate the fleet tracker correctly
  - "-" sentinel root_session is filtered out (would create one-row
    junk fleets for every direct curl)
  - LRU eviction kicks in at _FLEET_MAX
  - _fleet_summary aggregates calls/errors/max_depth/depth_histogram
  - /api/fleets and /api/fleets/{root_id} return the expected shape
"""

import importlib.util
import sys
from pathlib import Path


# ── Module loading (absolute-path import — dashboard isn't packaged) ──

_DASHBOARD_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts" / "dev" / "dashboard" / "dashboard.py"
)


def _load_dashboard():
    """Import dashboard.py freshly each time so module-level state
    (_FLEETS, METRICS) is reset between tests. Without this, ordering
    between test cases would leak fleets and break LRU assertions."""
    spec = importlib.util.spec_from_file_location("dashboard_under_test", _DASHBOARD_PATH)
    assert spec is not None and spec.loader is not None, f"missing {_DASHBOARD_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # Don't register under sys.modules — each test gets its own private copy.
    spec.loader.exec_module(mod)
    return mod


# ── Audit regex — legacy + new formats ─────────────────────────────────


def test_audit_regex_matches_legacy_format():
    """Pre-P1#3 audit lines (no lineage triple) MUST still match.

    We never wanted to break direct-curl traffic visibility — the regex
    has a non-capturing optional group around (root, parent, depth).
    """
    d = _load_dashboard()
    legacy = (
        "2026-05-29 10:00:00.000 [http-nio-8180-exec-1] INFO  AUDIT     "
        ": tenant=default user=alice tool=memory_set duration=13ms status=OK"
    )
    m = d._AUDIT_RE.search(legacy)
    assert m is not None, "legacy audit line failed to match"
    assert m.group("tool") == "memory_set"
    assert m.group("dur") == "13"
    assert m.group("status") == "OK"
    # Lineage fields absent from legacy line.
    assert m.group("root") is None
    assert m.group("parent") is None
    assert m.group("depth") is None


def test_audit_regex_matches_new_format_with_lineage():
    """Post-P1#3 lines carry root_session, parent_session, depth between
    tool= and duration=. The regex must extract all three plus the existing
    fields, and field order must be the locked one (root, parent, depth)."""
    d = _load_dashboard()
    new = (
        "2026-05-29 10:00:00.000 [http-nio-8180-exec-1] INFO  AUDIT     "
        ": tenant=default user=alice tool=memory_search "
        "root_session=chat-7 parent_session=chat-7/sub-abc depth=2 "
        "duration=42ms status=OK"
    )
    m = d._AUDIT_RE.search(new)
    assert m is not None, "P1#3 audit line failed to match"
    assert m.group("tool") == "memory_search"
    assert m.group("root") == "chat-7"
    assert m.group("parent") == "chat-7/sub-abc"
    assert m.group("depth") == "2"
    assert m.group("dur") == "42"
    assert m.group("status") == "OK"


def test_audit_regex_matches_new_format_with_dash_sentinel():
    """The Java side emits "-"/"-"/0 when no lineage headers were sent
    (e.g. a direct curl). The regex must match — we filter the sentinel
    at the call site, not via regex non-match."""
    d = _load_dashboard()
    sentinel_line = (
        "INFO AUDIT : tenant=default user=alice tool=memory_get "
        "root_session=- parent_session=- depth=0 "
        "duration=5ms status=OK"
    )
    m = d._AUDIT_RE.search(sentinel_line)
    assert m is not None
    assert m.group("root") == "-"
    assert m.group("depth") == "0"


def test_audit_regex_matches_fail_status():
    """FAIL lines have the same shape but with status=FAIL and an extra
    error= suffix we don't need to capture. Make sure the regex still
    matches and reports FAIL."""
    d = _load_dashboard()
    fail_line = (
        "INFO AUDIT : tenant=default user=alice tool=code_run "
        "root_session=chat-9 parent_session=chat-9 depth=0 "
        "duration=8ms status=FAIL error=sandbox timeout"
    )
    m = d._AUDIT_RE.search(fail_line)
    assert m is not None
    assert m.group("status") == "FAIL"
    assert m.group("tool") == "code_run"


# ── _ingest_line behavior — fleet tracker side effects ────────────────


def test_ingest_legacy_line_updates_tool_metrics_but_no_fleet():
    """A legacy AUDIT line should still bump per-tool calls/last_ms but
    must NOT create a fleet entry (no lineage information available)."""
    d = _load_dashboard()
    line = (
        "INFO AUDIT : tenant=default user=alice tool=memory_set "
        "duration=13ms status=OK"
    )
    d._ingest_line("memory", line)
    assert d.METRICS["memory"]["tools"]["memory_set"]["calls"] == 1
    assert d.METRICS["memory"]["tools"]["memory_set"]["last_ms"] == 13
    assert len(d._FLEETS) == 0, "legacy line must not create a fleet"


def test_ingest_new_line_creates_fleet_entry():
    """An AUDIT line with a real (non-dash) root_session creates a fleet
    with exactly one row, and the row carries all the lineage fields."""
    d = _load_dashboard()
    line = (
        "INFO AUDIT : tenant=default user=alice tool=memory_search "
        "root_session=chat-7 parent_session=chat-7 depth=0 "
        "duration=42ms status=OK"
    )
    d._ingest_line("memory", line)
    assert "chat-7" in d._FLEETS
    rows = list(d._FLEETS["chat-7"]["rows"])
    assert len(rows) == 1
    r = rows[0]
    assert r["depth"] == 0
    assert r["parent_session"] == "chat-7"
    assert r["component"] == "memory"
    assert r["tool"] == "memory_search"
    assert r["duration_ms"] == 42
    assert r["status"] == "OK"


def test_ingest_dash_sentinel_does_not_create_fleet():
    """The "-/-/0" sentinel means no fleet context. We must NOT create a
    junk "-" fleet — every direct curl would otherwise show up as its
    own one-row fleet, swamping the panel."""
    d = _load_dashboard()
    line = (
        "INFO AUDIT : tenant=default user=alice tool=memory_get "
        "root_session=- parent_session=- depth=0 "
        "duration=5ms status=OK"
    )
    d._ingest_line("memory", line)
    # Tool metrics still updated:
    assert d.METRICS["memory"]["tools"]["memory_get"]["calls"] == 1
    # But no fleet:
    assert len(d._FLEETS) == 0


def test_ingest_groups_rows_by_root_session():
    """Multiple audit rows with the same root_session collapse into one
    fleet entry, preserving chronological order."""
    d = _load_dashboard()
    for tool, depth, parent in [
        ("memory_search", 0, "chat-7"),
        ("file_read", 1, "chat-7/sub-abc"),
        ("file_read", 1, "chat-7/sub-abc"),
        ("memory_set", 0, "chat-7"),
    ]:
        line = (
            f"INFO AUDIT : tenant=default user=alice tool={tool} "
            f"root_session=chat-7 parent_session={parent} depth={depth} "
            f"duration=10ms status=OK"
        )
        d._ingest_line("memory", line)
    assert len(d._FLEETS) == 1
    rows = list(d._FLEETS["chat-7"]["rows"])
    assert [r["tool"] for r in rows] == [
        "memory_search", "file_read", "file_read", "memory_set"
    ], "rows must preserve chronological order"


# ── LRU eviction at _FLEET_MAX ────────────────────────────────────────


def test_fleet_eviction_lru_at_cap():
    """When more than _FLEET_MAX distinct fleets arrive, the OLDEST
    (least-recently-touched) one is evicted. A 'touch' (new row arriving
    on an existing fleet) moves it back to the most-recent end."""
    d = _load_dashboard()
    # Set a tiny cap for the test — bypassing the production default of 50.
    d._FLEET_MAX = 3

    def emit(root):
        d._ingest_line("memory", (
            f"INFO AUDIT : tenant=default user=alice tool=memory_set "
            f"root_session={root} parent_session={root} depth=0 "
            f"duration=1ms status=OK"
        ))

    emit("fleet-a")
    emit("fleet-b")
    emit("fleet-c")
    assert set(d._FLEETS.keys()) == {"fleet-a", "fleet-b", "fleet-c"}

    # New fleet → 'fleet-a' should evict (oldest in LRU).
    emit("fleet-d")
    assert "fleet-a" not in d._FLEETS
    assert set(d._FLEETS.keys()) == {"fleet-b", "fleet-c", "fleet-d"}

    # Touch 'fleet-b' (new row) — it moves to most-recent. Now a new
    # arrival should evict 'fleet-c' (which is now oldest), not 'fleet-b'.
    emit("fleet-b")
    emit("fleet-e")
    assert "fleet-c" not in d._FLEETS
    assert set(d._FLEETS.keys()) == {"fleet-b", "fleet-d", "fleet-e"}


# ── _fleet_summary aggregates ─────────────────────────────────────────


def test_fleet_summary_aggregates_calls_errors_depth():
    """Summary should report: number of calls, errors, max depth, and a
    histogram of {depth -> count} for the sparkline."""
    d = _load_dashboard()
    # Simulate a root with 1 OK call + 2 children where one fails.
    for tool, depth, status in [
        ("memory_search", 0, "OK"),
        ("file_read", 1, "OK"),
        ("file_read", 1, "FAIL"),
    ]:
        d._ingest_line("memory", (
            f"INFO AUDIT : tenant=default user=alice tool={tool} "
            f"root_session=chat-x parent_session=chat-x depth={depth} "
            f"duration=5ms status={status}"
        ))
    summary = d._fleet_summary()
    assert len(summary) == 1
    s = summary[0]
    assert s["root_session"] == "chat-x"
    assert s["calls"] == 3
    assert s["errors"] == 1
    assert s["max_depth"] == 1
    assert s["depth_histogram"] == {0: 1, 1: 2}


def test_fleet_summary_returns_newest_first():
    """The UI renders fleets top-down newest-first; the API must return
    them in that order."""
    d = _load_dashboard()
    for root in ["older", "middle", "newer"]:
        d._ingest_line("memory", (
            f"INFO AUDIT : tenant=default user=alice tool=memory_set "
            f"root_session={root} parent_session={root} depth=0 "
            f"duration=1ms status=OK"
        ))
    summary = d._fleet_summary()
    assert [s["root_session"] for s in summary] == ["newer", "middle", "older"]


# ── HTTP API endpoints ────────────────────────────────────────────────


def test_api_fleets_returns_empty_list_when_no_traffic():
    """Cold dashboard — no AUDIT lines seen — returns an empty list, not
    a 404 or an error. The UI distinguishes 'empty' from 'broken' via
    the response shape."""
    from fastapi.testclient import TestClient
    d = _load_dashboard()
    client = TestClient(d.app)
    r = client.get("/api/fleets")
    assert r.status_code == 200
    assert r.json() == {"fleets": []}


def test_api_fleets_returns_populated_after_ingest():
    """End-to-end: ingest a couple of audit lines, then hit /api/fleets
    and verify the aggregates surface."""
    from fastapi.testclient import TestClient
    d = _load_dashboard()
    for tool, depth in [("memory_search", 0), ("file_read", 1)]:
        d._ingest_line("memory", (
            f"INFO AUDIT : tenant=default user=alice tool={tool} "
            f"root_session=chat-api parent_session=chat-api depth={depth} "
            f"duration=7ms status=OK"
        ))
    client = TestClient(d.app)
    r = client.get("/api/fleets")
    assert r.status_code == 200
    fleets = r.json()["fleets"]
    assert len(fleets) == 1
    assert fleets[0]["root_session"] == "chat-api"
    assert fleets[0]["calls"] == 2
    assert fleets[0]["max_depth"] == 1


def test_api_fleet_detail_returns_rows_in_order():
    """Detail endpoint returns full rows in chronological order plus
    aggregates. Uses :path matcher to support slash-containing session ids."""
    from fastapi.testclient import TestClient
    d = _load_dashboard()
    # Use a slash-containing root id to defend against URL routing regressions.
    root = "chat-7/branch-2"
    for tool, depth in [("memory_search", 0), ("file_read", 1), ("memory_set", 0)]:
        d._ingest_line("memory", (
            f"INFO AUDIT : tenant=default user=alice tool={tool} "
            f"root_session={root} parent_session={root} depth={depth} "
            f"duration=3ms status=OK"
        ))
    client = TestClient(d.app)
    r = client.get(f"/api/fleets/{root}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root_session"] == root
    assert body["evicted"] is False
    assert [row["tool"] for row in body["rows"]] == [
        "memory_search", "file_read", "memory_set"
    ]
    assert body["calls"] == 3
    assert body["max_depth"] == 1


def test_api_fleet_detail_evicted_returns_empty_rows():
    """A fleet that's been evicted by LRU should return evicted=True with
    an empty rows list — the UI uses this to render a stale-selection
    message instead of a 404 error."""
    from fastapi.testclient import TestClient
    d = _load_dashboard()
    client = TestClient(d.app)
    r = client.get("/api/fleets/nonexistent-root")
    assert r.status_code == 200
    body = r.json()
    assert body["evicted"] is True
    assert body["rows"] == []
    assert body["root_session"] == "nonexistent-root"
