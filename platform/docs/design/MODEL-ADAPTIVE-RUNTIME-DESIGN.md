# Model-Adaptive Runtime Design

## Principle

The runtime must support local small models and stronger models without
rewriting the platform. Keep production safety invariant; make autonomy and
quality strategies configurable per agent and model profile.

```text
Invariant safety boundary
  auth / ACL / approval / idempotency / audit / checkpoint

Configurable runtime policy
  workflow strictness / tool exposure / context budget / verifier frequency

Replaceable model capability
  planning / reasoning / query rewrite / response quality
```

A stronger model may receive more planning freedom, but it must never bypass
authorization, tenant isolation, approval, side-effect protection, or audit.

## Capability Profiles

```yaml
capabilities:
  autonomy_level: constrained       # constrained | adaptive | autonomous
  planning: workflow                 # workflow | dynamic
  parallel_tools: false
  dynamic_replan: false

retrieval:
  query_rewrite: auto
  reranker: cross_encoder
  evidence_threshold: 0.65

verification:
  mode: adaptive                     # off | sample | adaptive | always
```

Example policy intent:

| Model profile | Workflow | Tools | Verification | Context |
|---|---|---|---|---|
| Local 7B Customer QA | strict | task-scoped | evidence always | compact |
| Strong Customer QA | adaptive | broader, policy-gated | adaptive | larger |
| Strong Coding Agent | dynamic | parallel reads, gated writes | tests on mutation | larger |

## What Must Stay Invariant

- authentication and tenant/ACL checks;
- policy decisions and human approval for risky actions;
- idempotency and lease protection for side effects;
- durable events, audit metadata, and checkpoint recovery;
- evidence provenance and version/freshness checks;
- terminal state and retry semantics.

These are system guarantees, not prompt tricks for weak models.

## What May Adapt

- maximum steps and planning depth;
- tool catalog shown to the model;
- query rewriting and clarification behavior;
- context/history/evidence budgets;
- parallel tool calls;
- verifier sampling and retry policy;
- amount of dynamic replanning allowed.

Avoid hard-coding small-model limitations into the Runtime. A strict workflow
should be a profile choice, not the only execution path.

## Evaluation Gate

Every profile change should compare the same task set across:

- task success and grounded-answer rate;
- refusal and escalation accuracy;
- side-effect and policy violations;
- tool-call count and latency;
- recovery and reconnect behavior;
- context cost and model output quality.

The platform should loosen a constraint only when the stronger profile improves
quality without regressing safety or recovery guarantees.
