"""Context Compression Eval Runner.

Executes the scenarios defined in scenarios.yaml against the compressor,
collects decision logs and metrics, and outputs a JSON report.

Usage:
    cd platform/eval-harness
    python -m run_compression_eval [--scenario debug_needle] [--output report.json]

Or from project root:
    cd platform/agent-server && .venv/bin/python ../eval-harness/scenarios/context_compression/run_eval.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Add agent-server to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "agent-server"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.compressor import (
    InvestigationState,
    compress_messages,
    estimate_tokens,
    update_investigation_from_messages,
)
from app.observability.decision_log import (
    CompressionEvent,
    DecisionLogger,
    FactExtractionEvent,
    StateSnapshot,
    TokenBudgetEvent,
)


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    metrics: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    duration_ms: int = 0


# ── Scenario Generators ──────────────────────────────────────────────────────


def generate_debug_needle_messages(needle_turn: int = 5, total_turns: int = 30):
    """Generate a 30-turn debug conversation with a needle at turn 5."""
    msgs = [HumanMessage(content="I'm getting a NullPointerException when saving users. Help me debug.")]

    for turn in range(1, total_turns + 1):
        if turn == needle_turn:
            # The needle — critical info
            msgs.append(AIMessage(content="Let me check the stack trace.", tool_calls=[{"id": str(turn), "name": "read_file", "args": {"path": "logs/error.log"}}]))
            msgs.append(ToolMessage(
                content="java.lang.NullPointerException at UserService.java:142\n"
                        "  at com.example.UserService.save(UserService.java:142)\n"
                        "  at com.example.UserController.createUser(UserController.java:58)\n"
                        "  at sun.reflect.NativeMethodAccessorImpl.invoke0(Native Method)",
                tool_call_id=str(turn), name="read_file", id=f"t{turn}",
            ))
        else:
            # Noise — exploring wrong paths
            noise_actions = [
                ("read_file", f"Checking config file...\n{'x' * random.randint(200, 800)}\nNo issues found here."),
                ("grep_search", f"Searching for pattern...\n{'y' * random.randint(300, 600)}\nNo matches."),
                ("read_file", f"Looking at imports...\n{'z' * random.randint(200, 500)}\nAll imports look correct."),
                ("run_test", f"Running tests...\n{'.' * 50}\nAll 15 tests passed."),
            ]
            tool_name, output = random.choice(noise_actions)
            msgs.append(AIMessage(content=f"Let me investigate path {turn}...", tool_calls=[{"id": str(turn), "name": tool_name, "args": {}}]))
            msgs.append(ToolMessage(content=output, tool_call_id=str(turn), name=tool_name, id=f"t{turn}"))

    return msgs


def generate_hypothesis_chain_messages():
    """Generate messages with progressive hypothesis elimination."""
    msgs = [HumanMessage(content="The /api/users endpoint returns 500. Help me find why.")]

    hypotheses = [
        ("config file missing", "read_file", "$ ls config/\nconfig.yaml  application.properties\nFile exists, config is fine."),
        ("wrong port", "run_test", "$ curl localhost:8080/health\n{\"status\":\"UP\"}\nPort is correct."),
        ("auth token expired", "run_test", "$ jwt decode $TOKEN\n{\"exp\": 1749200000}\nToken expires tomorrow, still valid."),
        ("race condition", "run_test", "$ ./gradlew test --tests UserServiceTest -i\nReproduces in single-threaded mode. NOT a race condition."),
        ("null injection", "read_file", "UserService.java:142:\n  user.getAddress().getCity() // address is NULL here!\nFOUND IT: address field is never set in the builder."),
    ]

    for i, (hyp, tool, output) in enumerate(hypotheses):
        msgs.append(AIMessage(content=f"Hypothesis: {hyp}. Let me verify.", tool_calls=[{"id": str(i), "name": tool, "args": {}}]))
        msgs.append(ToolMessage(content=output, tool_call_id=str(i), name=tool, id=f"t{i}"))
        if i < 4:  # eliminated
            msgs.append(AIMessage(content=f"❌ Ruled out: {hyp}. Moving on..."))
        else:
            msgs.append(AIMessage(content=f"✅ Confirmed: {hyp} is the root cause!"))

    # Add padding to push over budget
    for j in range(5):
        msgs.append(HumanMessage(content=f"What about checking {j}?"))
        msgs.append(AIMessage(content=f"Already covered in earlier investigation {'.' * 200}"))

    return msgs, hypotheses


def generate_multi_file_messages():
    """Generate messages discovering bugs across 4 files."""
    msgs = [HumanMessage(content="Users can't sign up. Find and fix all issues.")]

    files = [
        ("src/main/java/com/example/Controller.java", "Returns 200 instead of 201 on create", "read_file",
         "Controller.java:\n@PostMapping\npublic ResponseEntity<?> create() {\n  return ResponseEntity.ok(saved); // BUG: should be .created()\n}"),
        ("src/main/java/com/example/Service.java", "NPE on getId", "read_file",
         "Service.java:\npublic User save(User u) {\n  log.info(u.getId().toString()); // BUG: getId() is null before persist\n  return repo.save(u);\n}"),
        ("src/main/java/com/example/Repository.java", "Wrong finder method", "grep_search",
         "Repository.java:\n  Optional<User> findByEmail(String username); // BUG: parameter is username but method finds by email"),
        ("src/main/resources/db/migration/V3__add_column.sql", "Missing NOT NULL", "read_file",
         "ALTER TABLE users ADD COLUMN email VARCHAR(255); -- BUG: should be NOT NULL"),
    ]

    for i, (path, issue, tool, output) in enumerate(files):
        msgs.append(AIMessage(content=f"Checking {path}...", tool_calls=[{"id": str(i), "name": tool, "args": {"path": path}}]))
        msgs.append(ToolMessage(content=output, tool_call_id=str(i), name=tool, id=f"t{i}"))
        msgs.append(AIMessage(content=f"Found issue in {path}: {issue}"))

    # Padding
    for j in range(4):
        msgs.append(HumanMessage(content=f"Anything else? #{j}"))
        msgs.append(AIMessage(content=f"Let me double-check {'.' * 300}"))

    return msgs, files


# ── Eval Execution ────────────────────────────────────────────────────────────


def run_debug_needle() -> ScenarioResult:
    """Run the debug_needle scenario."""
    t0 = time.monotonic()
    logger = DecisionLogger()

    msgs = generate_debug_needle_messages(needle_turn=5, total_turns=25)
    before_tokens = estimate_tokens(msgs)

    # Log pre-compression snapshot
    logger.log_snapshot(StateSnapshot(
        session_id="eval-debug-needle", turn_number=22,
        event="pre_compression", message_count=len(msgs),
        token_estimate=before_tokens,
    ))

    # Track token budget
    logger.log_token_budget(TokenBudgetEvent(
        session_id="eval-debug-needle", turn_number=22,
        total_tokens=before_tokens, budget_tokens=1500,
    ))

    # Run compression
    investigation = InvestigationState()
    investigation = update_investigation_from_messages(investigation, msgs)

    compressed, summary = compress_messages(msgs, budget_tokens=1500, investigation=investigation)
    after_tokens = estimate_tokens(compressed)

    # Log compression event
    logger.log_compression(CompressionEvent(
        session_id="eval-debug-needle", turn_number=22,
        trigger="token_budget_exceeded",
        before_tokens=before_tokens, after_tokens=after_tokens,
        messages_dropped=len(msgs) - len(compressed),
        messages_summarized=1 if summary else 0,
        facts_retained=investigation.confirmed_facts,
    ))

    # Check: can we still find "142" and "UserService" in compressed output OR investigation state?
    all_content = " ".join(
        m.content if isinstance(m.content, str) else str(m.content)
        for m in compressed
    )
    # Investigation state is the primary retention mechanism
    investigation_text = investigation.to_summary_block()
    combined = all_content + " " + investigation_text

    has_142 = "142" in combined
    has_userservice = "UserService" in combined

    fact_recall = (int(has_142) + int(has_userservice)) / 2.0
    compression_ratio = after_tokens / before_tokens if before_tokens > 0 else 1.0

    elapsed = int((time.monotonic() - t0) * 1000)

    return ScenarioResult(
        name="debug_needle",
        passed=fact_recall >= 1.0,
        metrics={
            "fact_recall": fact_recall,
            "compression_ratio": round(compression_ratio, 3),
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "messages_before": len(msgs),
            "messages_after": len(compressed),
            "investigation_facts": investigation.confirmed_facts,
            "has_142": has_142,
            "has_UserService": has_userservice,
        },
        events=[e["data"] for e in logger.get_buffer()],
        duration_ms=elapsed,
    )


def run_hypothesis_chain() -> ScenarioResult:
    """Run the hypothesis_chain scenario."""
    t0 = time.monotonic()
    logger = DecisionLogger()

    msgs, hypotheses = generate_hypothesis_chain_messages()
    before_tokens = estimate_tokens(msgs)

    investigation = InvestigationState()
    investigation = update_investigation_from_messages(investigation, msgs)

    # Log fact extraction
    logger.log_fact_extraction(FactExtractionEvent(
        session_id="eval-hypothesis-chain", turn_number=18,
        new_facts=investigation.confirmed_facts,
        new_eliminations=investigation.eliminated,
        hypothesis_changed=True,
    ))

    compressed, summary = compress_messages(msgs, budget_tokens=1200, investigation=investigation)
    after_tokens = estimate_tokens(compressed)

    logger.log_compression(CompressionEvent(
        session_id="eval-hypothesis-chain", turn_number=18,
        trigger="token_budget_exceeded",
        before_tokens=before_tokens, after_tokens=after_tokens,
        messages_dropped=len(msgs) - len(compressed),
        messages_summarized=1 if summary else 0,
        facts_retained=investigation.confirmed_facts,
    ))

    # Check: does investigation state retain the eliminated hypotheses?
    eliminated_keywords = ["config", "port", "auth", "race"]
    investigation_text = investigation.to_summary_block().lower()
    all_content = " ".join(
        m.content if isinstance(m.content, str) else str(m.content)
        for m in compressed
    ).lower()
    combined = investigation_text + " " + all_content

    recalls = [kw in combined for kw in eliminated_keywords]
    chain_integrity = sum(recalls) / len(recalls)

    elapsed = int((time.monotonic() - t0) * 1000)

    return ScenarioResult(
        name="hypothesis_chain",
        passed=chain_integrity >= 0.75,
        metrics={
            "reasoning_chain_integrity": chain_integrity,
            "compression_ratio": round(after_tokens / before_tokens, 3) if before_tokens else 1.0,
            "eliminated_recalled": {kw: found for kw, found in zip(eliminated_keywords, recalls)},
            "investigation_eliminations": investigation.eliminated,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        },
        events=[e["data"] for e in logger.get_buffer()],
        duration_ms=elapsed,
    )


def run_multi_file() -> ScenarioResult:
    """Run the multi_file_debug scenario."""
    t0 = time.monotonic()
    logger = DecisionLogger()

    msgs, files = generate_multi_file_messages()
    before_tokens = estimate_tokens(msgs)

    investigation = InvestigationState()
    investigation = update_investigation_from_messages(investigation, msgs)

    compressed, summary = compress_messages(msgs, budget_tokens=1000, investigation=investigation)
    after_tokens = estimate_tokens(compressed)

    logger.log_compression(CompressionEvent(
        session_id="eval-multi-file", turn_number=16,
        trigger="token_budget_exceeded",
        before_tokens=before_tokens, after_tokens=after_tokens,
        messages_dropped=len(msgs) - len(compressed),
        messages_summarized=1 if summary else 0,
    ))

    # Check: how many file paths are still present in compressed output?
    all_content = " ".join(
        m.content if isinstance(m.content, str) else str(m.content)
        for m in compressed
    )
    file_paths = [f[0] for f in files]
    files_recalled = sum(1 for path in file_paths if path in all_content)

    elapsed = int((time.monotonic() - t0) * 1000)

    return ScenarioResult(
        name="multi_file_debug",
        passed=files_recalled >= 3,
        metrics={
            "file_coverage": files_recalled,
            "files_total": len(files),
            "coverage_ratio": files_recalled / len(files),
            "compression_ratio": round(after_tokens / before_tokens, 3) if before_tokens else 1.0,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "files_found": {path: path in all_content for path in file_paths},
        },
        events=[e["data"] for e in logger.get_buffer()],
        duration_ms=elapsed,
    )


# ── Main ──────────────────────────────────────────────────────────────────────


SCENARIOS = {
    "debug_needle": run_debug_needle,
    "hypothesis_chain": run_hypothesis_chain,
    "multi_file_debug": run_multi_file,
}


def run_all(scenario_filter: str | None = None) -> dict:
    """Run all (or filtered) scenarios and return report."""
    results = []
    scenarios_to_run = SCENARIOS
    if scenario_filter:
        scenarios_to_run = {k: v for k, v in SCENARIOS.items() if scenario_filter in k}

    for name, runner in scenarios_to_run.items():
        print(f"  Running: {name}...", end=" ")
        result = runner()
        status = "✅ PASS" if result.passed else "❌ FAIL"
        print(f"{status} ({result.duration_ms}ms)")
        results.append(asdict(result))

    total = len(results)
    passed = sum(1 for r in results if r["passed"])

    report = {
        "suite": "context_compression",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total else 0,
        },
        "results": results,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description="Context Compression Eval Runner")
    parser.add_argument("--scenario", "-s", help="Run specific scenario (substring match)")
    parser.add_argument("--output", "-o", default="report.json", help="Output report file")
    parser.add_argument("--dashboard", "-d", action="store_true", help="Generate HTML dashboard")
    args = parser.parse_args()

    print("🧪 Context Compression Eval Suite")
    print("=" * 50)

    report = run_all(args.scenario)

    # Write JSON report
    output_path = Path(args.output)
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n📄 Report written to: {output_path}")

    # Summary
    s = report["summary"]
    print(f"\n{'=' * 50}")
    print(f"Results: {s['passed']}/{s['total']} passed ({s['pass_rate']:.0%})")

    if args.dashboard:
        dashboard_path = output_path.with_suffix(".html")
        generate_dashboard(report, dashboard_path)
        print(f"📊 Dashboard: {dashboard_path}")

    return 0 if s["failed"] == 0 else 1


def generate_dashboard(report: dict, output_path: Path):
    """Generate an HTML dashboard from the report."""
    html = DASHBOARD_TEMPLATE.replace("{{REPORT_JSON}}", json.dumps(report, indent=2, default=str))
    output_path.write_text(html)


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Context Compression Eval Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 24px; }
  h1 { color: #58a6ff; margin-bottom: 8px; }
  .subtitle { color: #8b949e; margin-bottom: 24px; }
  .summary { display: flex; gap: 16px; margin-bottom: 32px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; flex: 1; }
  .card h3 { color: #8b949e; font-size: 12px; text-transform: uppercase; margin-bottom: 8px; }
  .card .value { font-size: 32px; font-weight: bold; }
  .pass { color: #3fb950; }
  .fail { color: #f85149; }
  .warn { color: #d29922; }
  .scenario { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 16px; }
  .scenario h2 { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .badge { padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
  .badge-pass { background: #1a3a2a; color: #3fb950; }
  .badge-fail { background: #3a1a1a; color: #f85149; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-top: 12px; }
  .metric { background: #0d1117; border-radius: 6px; padding: 12px; }
  .metric-name { font-size: 11px; color: #8b949e; text-transform: uppercase; }
  .metric-value { font-size: 20px; font-weight: bold; margin-top: 4px; }
  .events { margin-top: 16px; }
  .event { background: #0d1117; border-left: 3px solid #30363d; padding: 8px 12px; margin-bottom: 4px; font-size: 13px; font-family: monospace; }
  .event-compression { border-left-color: #a371f7; }
  .event-snapshot { border-left-color: #58a6ff; }
  .event-fact { border-left-color: #3fb950; }
  .chart { margin-top: 16px; height: 60px; background: #0d1117; border-radius: 6px; padding: 8px; display: flex; align-items: end; gap: 2px; }
  .bar { background: #58a6ff; border-radius: 2px 2px 0 0; min-width: 8px; }
  .bar-after { background: #3fb950; }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: #58a6ff; font-size: 13px; }
  pre { background: #0d1117; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<h1>🧪 Context Compression Eval</h1>
<p class="subtitle" id="timestamp"></p>

<div class="summary" id="summary-cards"></div>
<div id="scenarios"></div>

<script>
const report = {{REPORT_JSON}};

document.getElementById('timestamp').textContent = `Run: ${report.timestamp}`;

// Summary cards
const summary = report.summary;
document.getElementById('summary-cards').innerHTML = `
  <div class="card"><h3>Total</h3><div class="value">${summary.total}</div></div>
  <div class="card"><h3>Passed</h3><div class="value pass">${summary.passed}</div></div>
  <div class="card"><h3>Failed</h3><div class="value ${summary.failed > 0 ? 'fail' : ''}">${summary.failed}</div></div>
  <div class="card"><h3>Pass Rate</h3><div class="value ${summary.pass_rate >= 0.8 ? 'pass' : 'warn'}">${(summary.pass_rate * 100).toFixed(0)}%</div></div>
`;

// Scenarios
const container = document.getElementById('scenarios');
for (const result of report.results) {
  const badge = result.passed
    ? '<span class="badge badge-pass">PASS</span>'
    : '<span class="badge badge-fail">FAIL</span>';

  let metricsHtml = '<div class="metrics">';
  for (const [key, val] of Object.entries(result.metrics)) {
    if (typeof val === 'object') continue;
    let cls = '';
    if (typeof val === 'number') {
      if (key.includes('ratio') || key.includes('recall') || key.includes('integrity') || key.includes('coverage_ratio')) {
        cls = val >= 0.75 ? 'pass' : val >= 0.5 ? 'warn' : 'fail';
      }
    }
    metricsHtml += `<div class="metric"><div class="metric-name">${key}</div><div class="metric-value ${cls}">${typeof val === 'number' ? (val % 1 === 0 ? val : val.toFixed(3)) : val}</div></div>`;
  }
  metricsHtml += '</div>';

  // Token chart
  const before = result.metrics.before_tokens || 0;
  const after = result.metrics.after_tokens || 0;
  const maxT = Math.max(before, after, 1);
  const chartHtml = `
    <div class="chart">
      <div class="bar" style="height:${(before/maxT)*100}%; flex:1;" title="Before: ${before}"></div>
      <div class="bar bar-after" style="height:${(after/maxT)*100}%; flex:1;" title="After: ${after}"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-top:4px;">
      <span>Before: ${before} tokens</span><span>After: ${after} tokens</span>
    </div>`;

  // Events
  let eventsHtml = '';
  if (result.events && result.events.length > 0) {
    eventsHtml = '<details><summary>Decision Events (' + result.events.length + ')</summary><div class="events">';
    for (const evt of result.events) {
      const cls = evt.trigger ? 'event-compression' : evt.new_facts ? 'event-fact' : 'event-snapshot';
      eventsHtml += `<div class="event ${cls}">${JSON.stringify(evt)}</div>`;
    }
    eventsHtml += '</div></details>';
  }

  container.innerHTML += `
    <div class="scenario">
      <h2>${badge} ${result.name} <span style="font-size:12px;color:#8b949e;margin-left:auto;">${result.duration_ms}ms</span></h2>
      ${metricsHtml}
      ${chartHtml}
      ${eventsHtml}
    </div>`;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
