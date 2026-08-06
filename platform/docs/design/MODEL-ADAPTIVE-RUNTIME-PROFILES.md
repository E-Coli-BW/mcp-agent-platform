# Model-Adaptive Runtime Profiles

Profiles tune bounded execution for a model and domain pack: tool-call limit,
context budget, verification, and subagent permission. They do not configure
tenant isolation, approval policy, idempotency, or hidden-reasoning protection.
Those are runtime invariants and remain enforced independently of model size.

The Customer QA local 7B profile is intentionally read-only, capped at four
tool calls, and disallows subagents. A stronger model may receive a larger
context or more autonomy only after the same safety and eval gates pass.
