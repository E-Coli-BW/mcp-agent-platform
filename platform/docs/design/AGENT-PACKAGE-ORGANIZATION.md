# Agent Package Organization

> Status: complete. Slices 1-5 implemented; package reorganization closed.
> Created: 2026-08-15.
> Scope owner: `platform/agent-server/app/agent`.
>
> This document freezes the package reorganization requested after the Agent
> Runtime refactor. The work is behavior-preserving. New scope requires this
> document to be amended before implementation.

## 1. Problem

The runtime boundaries are now explicit, but the first level of `app/agent`
still mixes unrelated concepts:

- graph persistence and graph nodes;
- model request recovery;
- prompt and runtime policy;
- direct tool routing and required-tool postconditions;
- skill catalog and activation experiments;
- subagent execution, governance, verification, and event transport;
- classifier evaluation compatibility entry points.

Every file is a Python module, but the directory does not communicate bounded
contexts. A reader must already know the implementation before package names
become useful.

## 2. Goals

1. Make the first package segment identify the owning Agent capability.
2. Keep one production import path per symbol; do not add compatibility shims.
3. Preserve all runtime behavior, public functions, settings, event payloads,
   serialization contracts, and test outcomes.
4. Preserve lazy imports that prevent graph/tool/delegation cycles.
5. End with no behavior-bearing Python files at the `app/agent` root.

## 3. Target Tree

### Execution Ownership

```text
API adapters
  |
Runtime shell (RunCoordinator + platform capabilities + observers)
  |
AgentEngine (stable execution contract)
  |
registry/cache -> agent_graph
          |
   execution_policy | tool_routing | compressor
   model | skills | delegation
```

Dependencies point downward. Platform observers sit beside the coordinator and
cannot change Agent execution outcomes.

```text
app/agent/
  __init__.py

  agent_graph/
    builder.py
    checkpoint_store.py
    factory.py
    routing.py
    state.py
    nodes/
      context.py
      intent_router.py
      model.py
      reflexion.py
      tools.py
      verification.py

  compressor/
  engine/
  intent/
  model/
    factory.py
    fallback.py
    request_recovery.py

  execution_policy/
    __init__.py
    runtime_profiles.py
    system_prompts.py

  tool_routing/
    __init__.py
    direct_dispatch.py
    required_tools.py

  skills/
    __init__.py
    catalog.py
    triggered_activation.py

  delegation/
    __init__.py
    answer_verification.py
    executor.py
    governance.py
    progress_bus.py
```

Package `__init__.py` files remain empty unless a stable public API is already
owned by that package. In particular, `delegation/__init__.py` must not eagerly
import tool registries, models, or verifier code.

## 4. File Ownership Map

| Current file | Target owner |
|---|---|
| `checkpointing.py` | `agent_graph/checkpoint_store.py` |
| `classifier_eval.py` | delete; use `intent/evaluation.py` and `intent/eval_cli.py` |
| `fleet_bus.py` | `delegation/progress_bus.py` |
| `model_recovery.py` | `model/request_recovery.py` |
| `prompts.py` | `execution_policy/system_prompts.py` |
| `reflexion.py` | `agent_graph/nodes/reflexion.py` |
| `runtime_profile.py` | `execution_policy/runtime_profiles.py` |
| `skill_activation.py` | `skills/triggered_activation.py` |
| `skills_index.py` | `skills/catalog.py` |
| `subagent.py` | `delegation/executor.py` |
| `subagent_context.py` | `delegation/governance.py` |
| `subagent_verifier.py` | `delegation/answer_verification.py` |
| `tool_contract.py` | `tool_routing/required_tools.py` |
| `tool_router.py` | classification to `tool_routing/direct_dispatch.py`; graph adapter to `agent_graph/nodes/intent_router.py` |

## 5. Dependency Rules

1. `engine` may depend on graph, execution policy, and model owners.
2. `agent_graph` may depend on compressor, model, execution policy,
   tool-routing policy, and skills through narrow node imports.
3. `tool_routing` must not import LangGraph. The graph adapter belongs under
   `agent_graph/nodes`.
4. `execution_policy` must not import graph, tools, runtime, or HTTP adapters.
5. `skills` may lazily import memory clients, but package import itself must not
   perform network or tool-registry initialization.
6. `delegation` may depend on model, policy, and runtime context. Its executor
   keeps verifier and tool-registry imports lazy to avoid
   `tools.subagent_tool -> delegation.executor -> tool_registry` cycles.
7. Runtime/API/tool modules may import `delegation` submodules explicitly; they
   must not depend on root-level Agent compatibility exports.
8. No new root-level module may be added without amending this document.

## 6. Migration Slices

### Slice 1 - Policy, routing, and review

Status: complete (2026-08-15).

Scope:

- Move prompts and runtime profiles to `execution_policy/`.
- Split tool classification/policy from the LangGraph intent-router node.
- Move required-tool policy to `tool_routing/`.
- Move reflexion to `agent_graph/nodes/`.
- Delete the classifier-eval root facade and invoke its CLI from
  `intent/eval_cli.py`.

Exit criteria:

- `tool_routing` imports no LangGraph packages.
- Prompt, profile, routing, contract, classifier, reflexion, and graph tests pass.
- No active import references the old paths in this slice.

### Slice 2 - Graph persistence and model recovery

Status: complete (2026-08-15).

Scope:

- Move LangGraph checkpointer lifecycle under `agent_graph/`.
- Move model request retry/recovery under `model/`.
- Preserve the separate durable run-resume owner under `app/runtime`.

Exit criteria:

- Graph/model owner imports are acyclic in cold-import probes.
- Checkpoint, model recovery, crash-resume, and Engine tests pass.
- No active import references the old paths in this slice.

### Slice 3 - Skills

Status: complete (2026-08-15).

Scope:

- Group catalog/index behavior and trigger matching under `skills/`.
- Do not activate or remove dormant trigger behavior.

Exit criteria:

- Existing skills catalog behavior and graph context injection pass.
- Package import performs no network calls.
- No active import references the old paths in this slice.

### Slice 4 - Delegation

Status: complete (2026-08-15).

Scope:

- Atomically move subagent executor, governance context, answer verification,
  and progress bus under `delegation/`.
- Update all production imports and string-based test patch paths.
- Preserve lazy verifier, model, and tool-registry imports.

Exit criteria:

- Chat, cancellation, fan-in, lineage, subagent, verifier, and retry-resume tests
  pass.
- Cold import of `app.agent.delegation` does not load the tool registry or model
  providers.
- No active import references the old delegation paths.

### Slice 5 - Closeout

Status: complete (2026-08-15).

Scope:

- Update current docs and comments.
- Add a static root-layout and old-import regression test.
- Refresh generated package metadata only through the normal editable install if
  required; do not hand-edit generated metadata.

Exit criteria:

- `app/agent` root contains only `__init__.py` plus the documented packages.
- Full Agent Server tests pass.
- All touched Python files pass Ruff.
- Editor diagnostics and `git diff --check` pass.

## 7. Invariants

- OpenAI API and SSE contracts remain unchanged.
- Agent YAML, prompt text, and tool schemas remain unchanged.
- Graph topology and terminal reasons remain unchanged.
- Model retry budgets, fingerprints, leases, and reconciliation semantics remain
  unchanged.
- Subagent depth, fanout, token, deadline, cancellation, and verifier semantics
  remain unchanged.
- `InvestigationState` and `CompressionResult` serialization identities remain
  unchanged.

## 8. Non-Goals

- Splitting files internally based only on line count.
- Changing Agent, subagent, retrieval, or model behavior.
- Activating `triggered_activation.py` in production.
- Moving `app/tools/subagent_tool.py` or runtime control modules.
- Introducing root compatibility shims or broad re-export packages.
- General lint cleanup outside touched files.

## 9. Change Control

- Only one slice is active at a time.
- Each slice receives focused executable validation before the next begins.
- A slice stops when its exit criteria pass; unrelated cleanup is deferred.
- The final implementation is committed only after Slice 5 validation unless the
  repository owner explicitly requests per-slice commits.

## 10. Completion Record

- `app/agent` now contains only `__init__.py` and nine named capability
  packages: graph, compressor, delegation, engine, execution policy, intent,
  model, skills, and tool routing.
- Prompt governance/runtime profiles, required-tool/direct-dispatch policy,
  reflexion, graph checkpointing, model request recovery, skills, and all
  delegation capabilities moved to their documented owners without shims.
- Direct tool classification is independent of LangGraph/LangChain; its graph
  adapter is owned by `agent_graph/nodes/intent_router.py`.
- Delegation package import remains inert: it does not load executor, tool
  registry, or model providers.
- Static tests enforce the root layout, retired import paths, and tool-routing
  dependency rule.
- Focused acceptance: policy/routing 110 passed; recovery/checkpointing 27
  passed; skills 64 passed; delegation 79 passed.
- Contract gates: context 58 passed; Agent runtime 55 passed.
- Full Agent Server suite: 617 passed, 1 skipped.
- All touched Python files pass Ruff; editor diagnostics and
  `git diff --check` pass.
