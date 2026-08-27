# Coding Agent E2E Suite

This suite runs common coding workflows against a small, realistic repository
fixture. Every live run uses a fresh temporary Git repository and restores the
Agent's previous workspace afterward.

## Cases

- repository architecture summary;
- API documentation;
- Mermaid business-flow diagram;
- method-level item validation change;
- order-cancellation feature.

Mutation cases have hidden tests that are copied into the temporary workspace
only after the Agent has finished. A known-good control overlay proves each
hidden oracle before any live model is evaluated.

## Attribution

The runner reports the first failed layer:

- `harness`: fixture, public tests, hidden oracle, or control is invalid;
- `runtime`: HTTP, workspace provenance, durable event capture, or service failure;
- `model_runtime_protocol`: bounded tool protocol/loop failure;
- `capability_or_agent_policy`: runtime is healthy but answer/diff/tests fail;
- `none`: all deterministic postconditions pass.

This does not claim perfect causal identification of model weights versus Agent
prompt/policy. Use a second model family or a scripted provider control when that
distinction matters.

## Commands

```bash
cd platform/eval-harness

# No live stack or model required. Validates fixture and deterministic oracles.
../agent-server/.venv/bin/python -m eval.coding_e2e --preflight-only

# Fast live vertical slice: architecture + cancellation feature.
../agent-server/.venv/bin/python -m eval.coding_e2e --smoke

# Full five-case suite. Use at least three runs for model evidence.
../agent-server/.venv/bin/python -m eval.coding_e2e --runs 3
```

Reports are written under `runs/coding-e2e/<timestamp>/` by default.