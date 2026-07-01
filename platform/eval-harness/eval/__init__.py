"""Offline eval harness for the MCP agent.

Why this exists
---------------
The live ops dashboard at scripts/dev/dashboard/ answers two questions:

    1. Is the stack healthy?  (RT, errors, throughput)
    2. What is it costing?    (tokens, $, context pressure)

It does NOT answer "did the agent give the right answer?" — that needs a
*reference truth* (golden dataset), not live traffic. Putting quality
metrics on a live dashboard makes the dashboard noisy when you need it
most. Quality belongs in offline eval, with a CI gate that fails the PR
if today's prompt template regresses on yesterday's golden cases.

3-axis taxonomy:

    Axis           | Question                       | Where it lives
    ---------------|--------------------------------|------------------
    Plumbing       | Up, fast, error-free?          | dev dashboard
    Economics      | Tokens, $, context pressure?   | dev dashboard
    Trajectory     | Right tools, right answer?     | THIS HARNESS

Design principles
-----------------
1. **Golden cases are YAML, one per file.** Easy to read, easy to PR,
   easy to add. Each case declares the prompt + machine-checkable
   expectations (tools called, substrings in answer, budgets).

2. **Scorers are composable and ordered cheap-to-expensive.**
   Substring/regex scorers run first (free, deterministic), tool-trace
   scorers next (cheap), LLM-judge last (slow + costly + flaky). The
   harness short-circuits when a cheap scorer already passes.

3. **Trajectory is the source of truth.** We capture the WHOLE
   conversation (assistant turns, tool calls, results, final answer) so
   scorers operate on structured data, not on the final-answer string
   alone. This lets us say "the agent called memory_search 5 times in a
   row — broken" without LLM-judge.

4. **CI gate compares to a baseline.** A run produces summary.json with
   per-case pass/fail. `eval-gate` fails the PR if pass_rate < threshold
   OR if any case that PASSED in baseline now FAILS (regression).

5. **No live prod calls.** The harness spins up its own agent process
   via the existing scripts/dev scenarios, runs, tears down. Pass
   `--agent-url` to skip that and target an already-running agent.
"""
from __future__ import annotations

__version__ = "0.1.0"
