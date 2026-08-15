# Agent Runtime Boundary Refactor

> Status: complete. Phases 1-4 accepted and implemented; refactor closed.
> Created: 2026-08-14.
> Scope owner: `platform/agent-server`.
>
> This document freezes the refactor boundary and iteration sequence. Changes
> outside the active phase require this document to be amended and reviewed
> before implementation. The refactor stops when the phase exit criteria pass;
> it must not expand through opportunistic cleanup.

## 1. Problem

The Agent Server has the required capabilities, but ownership is concentrated
in a few large modules and split across incompatible abstraction levels:

- `app/agent/graph.py` owns provider construction, fallback, checkpointers,
  Agent caching, and the legacy v1 graph.
- `app/agent/graph_v2.py` owns state, context injection, model-view compression,
  routing, model calls, tool errors, reflexion integration, artifact synthesis,
  and graph assembly.
- `app/runtime/chat_stream.py` owns graph execution plus SSE projection,
  cancellation, approval fan-in, usage, model provenance, Kafka, retrieval
  feedback, run recording, and terminal state.
- `app/runtime/` also owns platform concerns that are valid around an Agent but
  are not the Agent loop itself: tenancy-aware policy, approvals, idempotency,
  durable runs, leases, and recovery.

The result is not primarily excessive total code. The problem is that a reader
cannot follow the Agent execution contract without also parsing platform and
transport concerns, while platform code imports the concrete LangGraph factory.

## 2. Refactor Goals

1. Give the Agent execution core one explicit boundary.
2. Keep auth, tenancy, policy, approval, persistence, observability, and HTTP as
   composable capabilities around that boundary.
3. Preserve all existing API, event, checkpoint, and agent YAML behavior during
   migration.
4. Make the default v2 path independently readable before removing v1.
5. Reduce reasons for each module to change; reducing total line count is not a
   primary goal.
6. Keep each phase reversible and executable with focused tests.

## 3. Non-Goals

- Replacing LangGraph.
- Adopting Cordis or copying DSH package structure mechanically.
- Rewriting auth, multitenancy, RAG, subagents, or the frontend.
- Changing prompts, tool schemas, model routing, event payloads, or public APIs.
- Deleting v1 before production and compatibility callers use the new boundary.
- Refactoring unrelated style or legacy code while moving a boundary.

## 4. Target Ownership Model

```text
HTTP / SSE / polling adapters                 app/api
                    |
                    v
Platform runtime shell                         app/runtime
run lifecycle | tenancy | policy | approval | idempotency
recovery | event log | observers | session lane
                    |
                    v
Agent Engine boundary                          app/agent/engine
resolve agent | invoke turn | inspect pending state | resume graph
                    |
          +---------+---------+
          |                   |
          v                   v
Agent graph                capability ports
state/nodes/routing        Model | Tools | Checkpoint
          |
          v
LangGraph adapter                               app/agent/graph_v2.py
```

### 4.1 Agent Engine

The Agent Engine owns only execution semantics:

- resolve an agent definition and graph instance;
- invoke one user turn;
- stream engine events;
- inspect pending graph nodes;
- resume a durable graph without adding another user message.

It does not own:

- JWT or tenant authentication;
- HTTP/SSE formatting;
- Kafka or OpenTelemetry emission;
- approval UI and policy decisions;
- durable run status or recovery leases;
- RAG feedback and eval bookkeeping.

### 4.2 Platform Runtime Shell

`app/runtime` wraps the Engine with operational guarantees:

- run identity and terminal status;
- ordered durable events;
- policy and approval;
- cancellation and session serialization;
- model/tool retry, idempotency, and crash recovery;
- observers for usage, provenance, Kafka, and retrieval feedback.

It may call the `AgentEngine` contract but must not import `graph.py` or
`graph_v2.py` directly.

### 4.3 API Adapters

`app/api` translates protocols only:

- authenticate and construct a platform request;
- choose streaming or non-streaming representation;
- map runtime events to OpenAI-compatible JSON/SSE;
- never own Agent state transitions or tool/model retry decisions.

## 5. Target Module Map

```text
app/agent/engine/
  contracts.py       # AgentExecutionRequest, AgentExecutionResult, Protocol
  langgraph.py       # only LangGraph-specific implementation
  __init__.py        # stable get_agent_engine() composition seam

app/agent/agent_graph/
  state.py           # AgentState and investigation state integration
  nodes/             # context, model, tools, verification nodes
  routing.py         # typed route decisions and terminal reasons
  builder.py         # v2 graph topology only

app/agent/model/
  factory.py         # provider-native ChatModel construction
  fallback.py        # provider fallback/circuit breaker

app/agent/agent_graph/checkpoint_store.py
  # checkpointer selection and lifecycle

app/runtime/
  runner.py          # platform run orchestration around AgentEngine
  observers/         # usage, provenance, Kafka, retrieval feedback
  stream.py          # runtime event subscription, not protocol formatting

app/api/
  chat.py            # request/response adapter
  openai_sse.py      # OpenAI wire projection
```

This map is directional. Modules are created only in the phase that owns them;
empty abstractions must not be added in advance.

`agent_graph/` is intentionally distinct from the public `graph.py` module.
Python cannot safely host a `graph.py` compatibility facade and a `graph/`
package behind the same import name during migration. The distinct internal
name keeps legacy imports deterministic until Phase 4 removes the facade.

## 6. Dependency Rules

1. `app/agent/engine` may import `app/agent/graph*`, model, registry, and
   checkpoint code.
2. `app/runtime` may import `app/agent/engine`; it must not import concrete graph
   modules after Phase 1.
3. `app/api` may import `app/runtime` and protocol DTOs; it must not import graph
   modules.
4. Agent graph code must not import FastAPI, SSE, Kafka producers, auth
   middleware, or HTTP request objects.
5. Platform observers consume runtime events; the engine does not call platform
   observers directly.
6. Tenant identity is explicit input at the Engine boundary. ContextVars remain
   an implementation detail during migration, not the boundary contract.
7. V1 compatibility exports may delegate to new owners, but no new production
   caller may depend on V1.

## 7. Migration Phases

## Phase 1 - Establish the Agent Engine facade

Status: complete (2026-08-14).

### Scope

- Add `app/agent/engine/{contracts,langgraph}.py` and stable exports.
- Move production graph invocation behind `AgentEngine`:
  - normal chat streaming;
  - durable model-retry resume.
- Keep `graph.py` factories, caches, and checkpointers in place temporarily.
- Keep emitted LangChain event dictionaries unchanged.
- Add contract tests with a fake Engine and focused LangGraph adapter tests.

### Explicitly out of scope

- Moving model factory/checkpointer/cache code.
- Splitting `chat_stream.py` observers.
- Changing graph topology or prompts.
- Deleting v1.

### Exit criteria

- `app/runtime` has no direct import of `app.agent.graph` or `graph_v2`.
- Chat and retry recovery pass through the same Engine contract.
- Normal turn and `ainvoke(None)` resume behavior are covered.
- Existing protocol, recovery, and full suites pass.

### Completion record

- Added the `AgentEngine` contract and LangGraph adapter under
  `app/agent/engine/`.
- Migrated normal chat streaming and durable model-retry resume to the Engine
  boundary.
- Added an import-boundary regression test for `app/runtime` and `app/api`.
- Phase acceptance: 22 passed.
- Full Agent Server suite: 600 passed, 1 skipped.
- Touched-file Ruff and `git diff --check`: passed. Repository-wide Ruff still
  reports pre-existing diagnostics outside the Phase 1 files; cleanup is not
  part of this refactor.

## Phase 2 - Separate platform runner from observers and wire adapters

Status: complete (2026-08-14).

### Scope

- Introduce a platform `RunCoordinator` around `AgentEngine`.
- Extract usage/provenance/Kafka/retrieval feedback into runtime observers.
- Keep SSE payloads byte-compatible.
- Leave `chat_stream.py` as a compatibility facade until callers migrate.

### Exit criteria

- Core turn orchestration can run without FastAPI, SSE, Kafka, or RAG feedback.
- Observer failures cannot alter Agent execution outcome.
- `chat_stream.py` contains only composition and compatibility exports.

### Completion record

- Added a transport-neutral `RunCoordinator` and `RuntimeStreamEvent` contract.
- Reduced `chat_stream.py` to request adaptation and OpenAI event projection.
- Extracted usage, model provenance, Kafka, and retrieval feedback into isolated
  runtime observers.
- Kept default observer wiring in `runtime/composition.py`; importing the runner
  alone does not load FastAPI, SSE, Kafka, RAG, feedback, or usage modules.
- Added observer-failure, coordinator lifecycle, wire projection, and dependency
  regression tests.
- Full Agent Server suite: 605 passed, 1 skipped.
- Phase 2 touched-file Ruff and `git diff --check`: passed.

## Phase 3 - Consolidate graph/model/checkpoint ownership

Status: complete (2026-08-14).

### Scope

- Move provider construction to `app/agent/model/factory.py`.
- Move checkpointer selection/lifecycle to
  `app/agent/agent_graph/checkpoint_store.py`.
- Move cache/agent resolution into the Engine implementation.
- Split v2 state, nodes, routing, and builder according to the target map.
- Preserve temporary imports from `graph.py` for tests and external callers.

### Exit criteria

- `graph_v2.py` is removed or reduced to a compatibility export module.
- Every new graph module has one reason to change.
- No circular import between engine, graph, model, and checkpointing.

### Completion record

- Moved provider construction and fallback/circuit-breaker ownership to
  `app/agent/model/`; the legacy fallback module is a compatibility facade.
- Moved LangGraph checkpointer selection and lifecycle to
  `app/agent/agent_graph/checkpoint_store.py`, separate from durable runtime
  checkpoints.
- Split v2 state, context/model/tool/verification nodes, routing, topology, and
  Agent-definition construction under `app/agent/agent_graph/`.
- Reduced `graph_v2.py` to compatibility exports and removed the former
  `graph.py`/`graph_v2.py` lazy import cycle.
- Moved Agent resolution, cache keys, tool-set invalidation, and config watcher
  invalidation to the Engine-owned registry. `graph.get_agent` now delegates.
- Added ownership regression tests and verified three cold import orders.
- Full Agent Server suite: 610 passed, 1 skipped.
- New owner modules and focused Phase 3 tests pass Ruff. Legacy modules touched
  only to redirect imports retain pre-existing lint diagnostics; unrelated lint
  cleanup remains outside this phase.

## Phase 4 - Retire v1 and compatibility shims

Status: complete (2026-08-14).

### Preconditions

- No production setting or documented deployment selects v1.
- Search and runtime telemetry show no v1 usage.
- All tests patch the Engine boundary rather than v1 factories.

### Readiness Decision

- Checked-in deployment and environment configuration does not select v1.
- The local durable database contains only an empty
  `agent_run_checkpoints` table. It has no persisted `run_started` timeline,
  so this repository cannot claim a telemetry observation window or prove zero
  historical v1 traffic from local data.
- On 2026-08-14 the repository owner explicitly requested completion of Phase
  4 in the current iteration. That request waives the compatibility release
  window and telemetry gate for deleting internal facades. Historical
  provenance schemas keep accepting `python-v1` events; this waiver does not
  rewrite or invalidate stored history.

### Scope

- Make the Engine registry unconditionally build the modular v2 graph.
- Remove the `create_react_agent` v1 path, `agent_graph_version` setting, and
  runtime/eval controls that select a graph version.
- Delete internal compatibility modules `graph.py`, `graph_v2.py`, and
  `model_fallback.py`; point production and tests at owning modules.
- Keep runtime provenance as the stable `python-v2` value while retaining v1
  in historical event schemas.
- Update current architecture documents, observability examples, and eval
  harness controls.

### Auxiliary Modularization Scope

The repository-owner request also includes a bounded cleanup of non-mainline
Agent and RAG code. It is part of Phase 4 and is limited to these ownership
changes:

```text
app/agent/intent/
  __init__.py       # stable exports formerly provided by intent.py
  meta.py           # meta-question detection and direct answers
  routing.py        # topic-switch and complexity classification
  evaluation.py     # classifier cases, adapters, and result contracts
  eval_cli.py       # live inference, rendering, and result persistence

app/agent/intent/eval_cli.py
  # classifier evaluation CLI entry point

app/agent/compressor/
  __init__.py       # stable old import path and serialized class identity
  investigation.py # InvestigationState and fact extraction
  messages.py      # message budgets and compression policy

app/rag/eval/
  ranking.py              # reusable ranking metrics/evaluation
  compression_cases.py    # benchmark fixtures only
  compression_metrics.py  # compression baselines and scoring
  benchmark_compression.py # thin benchmark CLI
  benchmark_rerankers.py   # thin strategy benchmark CLI
  dataset.py               # stable BENCHMARK/CLI exports
```

### Auxiliary Non-Goals

- No subagent, fleet bus, verifier, model-recovery, or graph-topology changes.
- No production retriever, indexer, embedding, or reranker behavior changes.
- No broad lint cleanup or deletion of heuristic APIs merely because their
  current call count is low.
- No changes to prompt text, tool schemas, public HTTP/SSE contracts, or YAML
  AgentConfig.

### Ordered Slices

1. Add characterization tests and retire the v1 runtime/configuration path.
2. Retarget production/tests to owners and delete compatibility modules.
3. Convert intent and compressor modules into stable-export packages.
4. Extract reusable RAG eval cases/metrics while preserving benchmark CLIs.
5. Update current docs/eval controls and add a static no-v1 regression guard.

### Exit criteria

- One production graph path.
- No v1 cache, prompt-modifier, or factory branches remain.
- No active production, test, deployment, or current-document import depends
  on the three deleted compatibility modules.
- Intent, compressor, and RAG eval packages retain their existing public
  behavior through focused characterization tests.
- Full Agent Server tests, touched-file Ruff, editor diagnostics, and
  `git diff --check` pass.

### Completion record

- Removed the graph-version setting and every production/eval branch that could
  select the legacy v1 graph. Runtime provenance is now always `python-v2`.
- Deleted `graph.py`, `graph_v2.py`, and `model_fallback.py`; production and
  tests import the Engine, modular graph, model, and checkpoint owners directly.
- Updated config watching, subagent tool truncation, logging, observability,
  current architecture docs, and eval controls for the single graph path.
- Added a static retirement guard covering active application, test, eval, and
  current-document surfaces. Historical provenance schemas continue accepting
  `python-v1` records.
- Converted `intent.py` into a stable-export package and separated meta answers,
  routing heuristics, classifier evaluation, and its CLI.
- Converted `compressor.py` into investigation and message-policy modules while
  preserving `app.agent.compressor` class identity and pickle compatibility.
- Split RAG eval fixtures, compression metrics, ranking metrics, and benchmark
  CLIs. Ranking evaluation now owns its Python 3.13 event-loop lifecycle, and
  the reranker benchmark calls the explicit heuristic implementation.
- Explicit non-goals remained untouched: subagent behavior, model recovery,
  graph topology, and production retrieval/reranking behavior.
- Final Agent Server suite: 614 passed, 1 skipped.
- All Phase 4 Python files pass Ruff; editor diagnostics, eval Python/shell
  syntax checks, and `git diff --check` pass.

## 8. Invariants During Migration

Every phase must preserve:

- OpenAI-compatible streaming and non-streaming responses;
- tenant-isolated agent cache behavior;
- prompt-version cache isolation;
- approval and tool authorization before side effects;
- tool idempotency and durable model-retry recovery;
- cancellation and session lane semantics;
- runtime event ids and terminal reasons;
- current agent YAML and plugin contracts.

No phase may combine code movement with behavior changes unless the behavior
change is separately documented, tested, and explicitly added to this document.

## 9. Validation Matrix

Each phase runs:

1. Focused contract tests for the new boundary.
2. Chat protocol and prompt/tenant cache tests.
3. Approval, cancellation, and retry recovery tests affected by the slice.
4. Full `platform/agent-server` pytest suite.
5. Ruff on every touched Python file.
6. `git diff --check` and editor diagnostics.

For Phase 1 specifically:

```text
tests/test_agent_engine.py
tests/test_chat_protocol.py
tests/test_prompt_cache_isolation.py
tests/test_temperature_plumbing.py
tests/test_model_retry_resume_executor.py
tests/test_runtime_model_retry_crash_recovery_script.py
```

## 10. Change Control

- Only one phase may be in progress.
- Before implementation, update `Status` and the active phase scope here.
- New cleanup ideas are recorded under a future phase; they are not executed in
  the current patch.
- A phase ends when its exit criteria pass. Further optimization requires a new
  document revision and a separate change.
- Commits should name the boundary moved, not claim a broad runtime rewrite.
