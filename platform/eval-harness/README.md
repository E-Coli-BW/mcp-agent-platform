# agent-eval-harness

Offline quality eval for the MCP agent. Pytest + golden YAML cases +
composable scorers + a CI gate that fails the PR when this PR regresses
a previously-passing case.

## Why this is separate from the dashboard

The live ops dashboard (`scripts/dev/dashboard/`) answers two
questions:

1. **Is the stack healthy?** — RT, errors, throughput
2. **What is it costing?**   — tokens, $, context pressure

It deliberately does NOT answer "did the agent give the right answer?".
That needs a *reference truth* (golden dataset), not live traffic. The
3-axis taxonomy is in `scripts/dev/README.md`. This harness is axis 3.

## What it does

```
golden/*.yaml    →  cases (prompt + machine-checkable expectations)
                       │
                       ▼
                  runner.py        ──hits──►  agent at :8580
                       │                       │
                       │      ◄──trajectory────┘
                       ▼
              scorers/*.py    →  Score(name, passed, detail)
                       │
                       ▼
                  runs/<ts>/
                  ├── summary.json   ── machine-readable
                  └── report.md      ── for humans / PRs
                       │
                       ▼
                  ci/eval_gate.py    compares to baselines/master.json
                                     fails the build on regression
```

## Layout

```
platform/eval-harness/
├── pyproject.toml
├── README.md
├── golden/                           # one YAML per case
│   ├── memory_set_then_search.yaml
│   ├── code_run_basic.yaml
│   ├── no_tool_simple_chat.yaml
│   └── explain_recursion.yaml        # judge-graded
├── eval/
│   ├── __init__.py
│   ├── case.py                       # Case, Trajectory, Score, RunResult
│   ├── runner.py                     # loads cases → hits agent → scores
│   ├── report.py                     # markdown rendering
│   └── scorers/
│       ├── __init__.py               # SCORERS registry
│       ├── transport.py              # HTTP succeeded?
│       ├── budgets.py                # within latency/token budgets?
│       ├── tool_selection.py         # called the expected tools?
│       ├── answer_grounded.py        # answer mentions required facts?
│       └── llm_judge.py              # stronger LLM grades open-ended quality
├── suites/                           # public dataset adapters (not agent-loop)
│   └── bfcl/                         # Berkeley Function Calling Leaderboard
│       ├── __init__.py               # single-turn tool-call eval against the LLM
│       └── data/
│           └── simple_starter.json   # 10 bundled cases; download more separately
├── ci/
│   ├── __init__.py
│   └── eval_gate.py                  # CI hook: pass-rate floor + regression check
├── tests/                            # pure-logic unit tests
│   ├── test_scorers.py
│   ├── test_llm_judge.py
│   └── test_bfcl.py
└── baselines/                        # committed baselines for CI gate
    └── master.json                   # update after deliberate eval changes
```

## Quick start

```bash
# 1. Bring up the stack (auth + memory + codeexec + agent + dashboard).
./scripts/dev/scenarios/with-tools/up.sh

# 2. Install the harness.
cd platform/eval-harness
../agent-server/.venv/bin/pip install -e .

# 3. Run the harness.
../agent-server/.venv/bin/eval-run

# Output:
#   running 3 case(s) against http://localhost:8580 …
#   ✅ memory_set_then_search  (12,491ms)
#   ✅ code_run_basic          (3,872ms)
#   ✅ no_tool_simple_chat     (894ms)
#   results: 3/3 passed (100%) → runs/2026-05-29T10-23-12

# 4. Inspect the report.
cat runs/2026-05-29T10-23-12/report.md

# 5. Promote this run to the baseline (when you've validated it).
cp runs/2026-05-29T10-23-12/summary.json baselines/master.json

# 6. In CI: gate against the baseline.
../agent-server/.venv/bin/eval-gate runs/<latest>/summary.json \
    --baseline baselines/master.json --min-pass-rate 0.90
```

## n-of-k flakiness tolerance

LLM-in-loop evals at T>0 are non-deterministic. A single run is **not
evidence** — our experience during the subagent fleet rollout showed how
single-run "greens" can be misleading (one case shipped with a 1-of-1 lucky
run; the real pass rate at clean HEAD was 3/5).

The harness supports n-of-k via two knobs:

```bash
# Run each case 5 times; case passes iff all 5 pass (default threshold = 1.0)
../agent-server/.venv/bin/eval-run --case subagent_parallel_file_summary --runs 5

# Run each case 5 times; case passes iff >= 60% (3/5) pass
../agent-server/.venv/bin/eval-run --case subagent_parallel_file_summary \
    --runs 5 --pass-threshold 0.6
```

Or set a per-case threshold in the YAML (recommended for known-flaky
cases — keeps CI honest while not failing the build on coin-flip
non-determinism):

```yaml
expected:
  final_must_contain: ["embarrassingly parallel"]
  # On Qwen2.5:7b @ T=0.7 this case is empirically 3/5. Document the
  # ceiling here; bump back to 1.0 once we move to a less-flaky model
  # or fix the temperature plumbing.
  min_pass_rate: 0.6
```

Resolution priority: `--pass-threshold` > YAML `min_pass_rate` > default 1.0.

The CI gate (`eval-gate`) keys off the *aggregate* `passed` boolean, so a
case with `min_pass_rate: 0.6` that passes 3/5 is treated as PASS by the
gate — no special casing needed.

## Writing a case

A case is a YAML file under `golden/`. The id MUST be unique.

```yaml
id: my_new_scenario
description: One-sentence summary that appears in the report.
tags: [smoke, memory]              # optional; for `eval-run --tag smoke`

prompt: |
  The full user message sent to the agent. Multi-line supported.

expected:
  # ── trajectory (the "did it call the right tools?" axis) ──
  tools_called:         [memory_set, memory_search]   # each must appear ≥1×
  tools_called_min:     2                              # generous floor
  tools_called_max:     6                              # ceiling — catches loops
  tools_forbidden:      [code_shell]                   # MUST NOT call

  # ── final answer (the "did it ground its reply?" axis) ──
  final_must_contain:        ["blue"]                  # case-insensitive substring
  final_must_not_contain:    ["I cannot", "error"]
  final_regex:               '\b\d+\b'                 # advanced: any digits

  # ── budgets (catches cost/perf regressions) ──
  max_latency_ms:        60000
  max_prompt_tokens:     3000
  max_completion_tokens: 500
```

**Every `expected.*` field is OPTIONAL.** A scorer is skipped when its
field is absent — so a budget-only case can leave `tools_called` empty,
and a tool-trace-only case can leave the budgets empty.

## Design principles

1. **Golden cases are YAML, one per file.** Easy to PR. Easy to grep.
2. **Scorers are composable and ordered cheap-to-expensive.**
   Substring/regex first, tool-trace next, LLM-judge last (optional, paid).
3. **Trajectory is the source of truth.** We parse the agent's
   `<!-- TOOL:{...} -->` markers to reconstruct the full call graph, so
   scorers operate on structured data, not on the LLM's prose alone.
4. **CI gate compares to a baseline.** A case that PASSED yesterday and
   FAILS today is a *regression* — distinct from a brand-new failing
   case. The gate distinguishes the two.
5. **No mocked LLM.** Mocking would hide the bugs we care about
   (prompt-template breaks, tool-registration bugs, JWT propagation).
   The harness assumes a live stack and tells you to start one.

## Adding a new scorer

```python
# eval/scorers/my_scorer.py
from eval.case import Case, Score, Trajectory
from eval.scorers import register

@register
def my_scorer(case: Case, traj: Trajectory) -> Score | None:
    if not case.expected.my_field:           # opt-in
        return None
    ok = ...                                 # your logic
    return Score(name="my_scorer", passed=ok, detail="...")
```

Then add `my_scorer,` to the `from eval.scorers import (...)` block in
`eval/scorers/__init__.py`. Add an `Expectations` field in
`eval/case.py`. Add unit tests in `tests/test_scorers.py`. That's it.

## What this harness does NOT do (yet)

- **No parallel execution by default.** `--concurrency 1` because
  agent state is sticky. Crank up for throughput eval only.
- **No flake retry.** A flaky case should be either fixed or removed
  from the golden set. Retrying hides bugs.
- **No replay-from-trace.** The runner always hits a live agent.
  Replaying recorded trajectories against new scorers would be nice
  but isn't built yet.
- **Only BFCL "simple" category.** Multi-tool / parallel categories
  need a different scoring shape; not built yet.

---

## LLM-as-judge (advanced — open-ended quality)

For cases where deterministic scorers genuinely can't express what
"good" means (clarity, reasoning, tone), opt in via `expected.judge`.

```yaml
expected:
  # Cheap scorers still apply on top of the judge
  max_latency_ms: 60000
  final_must_contain: ["recursion"]

  judge:
    rubric: |
      Good: uses an analogy BEFORE code, mentions trade-offs.
      Poor: dumps code first, no "when not to use".
    criteria: [analogy_quality, reasoning, completeness, tone]
    min_score: 3.5             # 1–5 Likert, median across samples
    samples: 3                 # self-consistency
```

Cases without `judge:` are unaffected. Cases WITH `judge:` are
**soft-skipped** (no score emitted) when no judge backend is configured
— so judge-only cases don't break local dev runs for people without
an API key.

Configuration (env vars):

```bash
# Required to activate the judge:
export EVAL_JUDGE_MODEL="openai/gpt-4o"           # or anthropic/claude-3.5

# Optional (sensible defaults):
export EVAL_JUDGE_BASE_URL="https://api.openai.com/v1"   # or http://localhost:11434/v1
export EVAL_JUDGE_API_KEY="sk-..."                       # or "ollama"
export EVAL_JUDGE_SAMPLES=3                               # self-consistency N
export EVAL_JUDGE_TEMPERATURE=0.3
```

**Non-negotiable best practices** (see `eval/scorers/llm_judge.py`
for the full rationale):

1. Judge model should be a **different family** than the SUT (avoid
   self-grading bias of ~15%).
2. Judge model should be at least as **strong** as the SUT.
3. Always use a **structured rubric + per-criterion sub-scores**
   (1–5 Likert with anchors), never "rate 1–10".
4. Always sample N≥3 with low temperature for self-consistency.
5. Calibrate quarterly against ~50 human-labeled examples.

---

## BFCL — public function-calling benchmark

[BFCL (Berkeley Function Calling Leaderboard)](https://gorilla.cs.berkeley.edu/leaderboard.html)
tests whether the **underlying LLM** picks the right function and
constructs correct arguments — independent of our agent loop.

When to look at BFCL signal:
- `eval-run` fails → bug in OUR agent (prompt template, tool wiring,
  planner config, …).
- `eval-suite-bfcl` fails → the underlying model is weak at function
  calling; consider a different model OR provide more examples.

```bash
# 10 hand-picked cases ship in the repo (suites/bfcl/data/).
export EVAL_BFCL_MODEL=qwen2.5:7b
export EVAL_BFCL_BASE_URL=http://localhost:11434/v1
export EVAL_BFCL_API_KEY=ollama

../agent-server/.venv/bin/eval-suite-bfcl

# Smoke test:
../agent-server/.venv/bin/eval-suite-bfcl --limit 3

# Only "simple" category:
../agent-server/.venv/bin/eval-suite-bfcl --category simple
```

The bundled 10 cases cover the common LLM failure modes: type
confusion (int/float/bool), enum casing, optional arguments, nested
arrays, the "must NOT call any tool" case (`__none__` sentinel), and
ambiguous unit inference.

A failing BFCL case is highly diagnostic: the `summary.json` records
the *exact* predicted name + arguments, so you can see whether the
model called the wrong tool, missed a required argument, or got an
enum value wrong.

To grow the suite beyond 10 cases, drop more BFCL JSON files under
`suites/bfcl/data/`. The format is in `suites/bfcl/__init__.py::BfclCase`.
