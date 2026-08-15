# Agent Runtime Refactor Roadmap

> Status: public architecture baseline, created 2026-07-31.
> Scope: document the current runtime shape, identify split-brain areas, and define
> small reversible improvements before larger refactors.

## 1) Why This Exists

The project already has many production-shaped pieces: LangGraph v2, tool
registry, MCP-style Java backends, JWT tenancy, Redis conversation history,
prompt provenance, Kafka events, eval harness, subagent streaming/cancel, and
domain migration notes.

The current weak spot is not lack of features. It is that runtime semantics are
spread across API streaming code, graph code, stores, middleware, tool wrappers,
and observability helpers. Before adding more features, we need a shared
contract for runs, steps, checkpoints, policy decisions, and events.

## 2) Current Architecture Snapshot

## 2.1 End-to-end request path

Current primary path:

```text
POST /v1/chat/completions
  -> app/api/chat.py
  -> auth dependency + tenant_context
  -> request_id/run_id/session_id ContextVars
  -> RunCoordinator
  -> AgentEngine + Engine-owned registry/cache
  -> agent_graph/ explicit StateGraph
  -> LangGraph event stream
  -> local tools and MCP REST bridge tools
  -> chat.py converts graph events into SSE chunks/tool events
  -> conversation store append + usage/provenance/Kafka emissions
```

Important files:

- `platform/agent-server/app/api/chat.py`
- `platform/agent-server/app/agent/engine/`
- `platform/agent-server/app/agent/agent_graph/`
- `platform/agent-server/app/runtime/runner.py`
- `platform/agent-server/app/tools/definitions.py`
- `platform/agent-server/app/tools/mcp_client.py`
- `platform/agent-server/app/store/conversation.py`
- `platform/agent-server/app/middleware/session_lane.py`
- `platform/agent-server/app/events/model_provenance.py`

## 2.2 Current runtime graph

The runtime has one graph implementation under `app/agent/agent_graph/`.
There is no graph-version switch or legacy v1 production path.

Current v2 graph:

```text
START
  -> inject_context
  -> update_investigation_initial
  -> intent_router
      -> tools -> track_errors -> update_investigation_tools -> call_llm
      -> call_llm
  -> maybe_critique
  -> route
      -> tools
      -> hallucination_fix -> call_llm
      -> END
```

`call_llm` constructs an ephemeral model-facing view through
`_build_llm_view`. That view compresses older messages, injects structured
investigation facts, and applies one character budget across the system prompt,
skills, workspace context, and conversation. It is never merged back through
the `add_messages` reducer. If the required system/goal/recent-message floor
cannot fit, the call fails explicitly with `ContextBudgetExceededError`.

State fields include:

- `messages`
- `workspace_context`
- `skills_catalog`
- `loop_counter`
- `consecutive_errors`
- `critique_attempts`
- `investigation: InvestigationState`
- `compression_summary`

## 2.3 Current persistence and state

There are multiple state mechanisms:

| State | Current owner | Storage | Notes |
|---|---|---|---|
| Graph checkpoint | `app/agent/agent_graph/checkpoint_store.py` | Redis/Postgres if env set, else MemorySaver | Stores lossless messages plus typed investigation state; model-view compression is not persisted |
| Chat history | `ConversationStore` | Redis list + local LRU fallback | Separate from graph checkpoint |
| Request metadata | ContextVars | process-local | `request_id`, `run_id`, `session_id`, `prompt_version` |
| Model provenance | `model_provenance.py` + Kafka producer | Kafka best effort | Strong schema direction |
| Tool audit | Kafka producer + Java audit aspects | Kafka/logs/backend | Useful, but no unified tool invocation envelope yet |
| Subagent events/cancel | `fleet_bus.py` | in-process | Good for local streaming, not durable |

## 2.4 Current configuration surfaces

Main surfaces:

- `app/config.py`: env-driven runtime settings.
- `agents/*.yaml`: agent identity, model, prompt, tools, guardrails, routing.
- `app/registry/tool_registry.py`: registered LangChain tools.
- `app/agent/execution_policy/system_prompts.py`: prompt versions and governance resolution.
- MCP backend URLs in settings: memory, filesearch, codeexec.
- Feature flags: graph version, direct tool routing, reflexion, subagent verifier,
  reranker strategy, prompt canary.

## 3) Split-Brain / Friction Points

## 3.1 `run_id` exists but is not the runtime primary key

`run_id` is generated in `chat.py` and used by model provenance, but the runtime
does not persist a `Run` row/state machine keyed by it. There is no authoritative
place to ask:

- Is this run running, paused, cancelled, failed, or complete?
- Which step is active?
- Can I resume it?
- Which events belong to it?
- Which tool calls have already committed?

## 3.2 Checkpoint and conversation history are dual-track

LangGraph checkpoint state and `ConversationStore` both represent parts of
conversation state. They can diverge after restart, Redis outage, MemorySaver
fallback, or future multi-worker deployment.

Near-term stance: keep both, but document ownership clearly.

Target stance:

- Checkpointer owns graph execution state.
- Runtime event log owns run/step/event/audit state.
- Long-term memory store owns user/domain facts.
- Chat transcript is derived or explicitly persisted as a runtime artifact, not
  an unrelated side channel.

## 3.3 SSE is coupled to active execution

`chat.py` currently executes the agent and formats SSE in the same generator.
If the client disconnects, the task is cancelled. This is simple, but it makes
reconnect/resume difficult and makes the stream the source of truth.

Target stance:

- Runtime emits ordered `RunEvent` records.
- SSE/WebSocket/polling only read from the event log.
- `Last-Event-ID` or equivalent cursor can replay missed events.

## 3.4 Tool calls have a first unified execution boundary

Main-agent v1/v2 and subagent tools now pass through
`wrap_tool_with_authorization()` immediately before the underlying LangChain
tool operation. The wrapper builds a `ToolInvocationEnvelope`, evaluates
policy, and enforces deny/approval before side effects. `McpToolClient` remains
the remote transport below that boundary.

The first boundary carries:

- `tool_invocation_id`
- `run_id`
- `step_id`
- `tenant_id`
- normalized args hash
- idempotency key
- risk level
- approval status
- timeout/retry policy
- committed result for approval checkpoints

Remaining work is to make timeout/retry metadata and durable idempotency uniform
for every tool, rather than the current focused mutating-tool implementations.

## 3.5 Policy is distributed across layers

Current policy-like logic lives in several places:

- prompt rules in `agents/coding-agent.yaml`
- `max_tool_calls`, `max_context_chars`, `max_tool_output`
- JWT middleware and tenant context
- workspace path validation in tools
- direct tool router allowlist
- reflexion/verifier flags
- subagent depth/budget/tool allowlist

These are valuable, but not queryable as one `PolicyDecision`.

Target stance:

```text
PolicyDecision {
  decision: allow | deny | require_approval | redact | escalate
  reason
  risk_level
  policy_version
  matched_rules
}
```

## 3.6 Cancellation is cooperative and partly in-process

Subagent cancel is intentionally cooperative and in-process. This is okay for
the current feature, but not enough for a durable runtime. Long-running tools,
remote backend calls, and paused approvals need durable cancellation state.

## 3.7 Domain migration currently relies too much on prompt/tools

`AgentConfig` can swap model, prompt, and tools. That is necessary, but not
sufficient for health/customer QA. Domain migration also needs policies,
memory rules, retrieval ACLs, eval suites, handoff rules, and risk taxonomy.

## 4) Target Runtime Contract

## 4.1 First-class entities

```text
Thread
  Durable conversation/task container.
  Keyed by tenant_id + thread_id.

Run
  One execution attempt triggered by a user/API event.
  Has status: queued | running | paused | completed | failed | cancelled.

Step
  Atomic runtime unit inside a run.
  Types: input_guardrail, llm_call, tool_call, retrieval, verifier,
  approval, handoff, compression, final_output.

Checkpoint
  Durable graph/runtime state before or after a step.

RunEvent
  Ordered append-only event for streaming, replay, audit, and eval.

PolicyDecision
  Stored decision explaining why a step was allowed, denied, paused, or escalated.

ToolInvocation
  Tool execution envelope with idempotency and approval metadata.
```

## 4.2 Minimal schema direction

Start with Postgres tables or a durable SQLite/Postgres-compatible store in dev.
Redis can remain a cache/lock layer, but should not be the only source of truth.

Implementation status:

- Done: `RuntimeStore` contract, SQLite-backed `agent_runs`,
  `agent_run_events`, and `agent_tool_invocations` schema, plus a
  `StoreBackedRunRecorder` adapter.
- Done: restart-style unit test proving run events can be read through a fresh
  store/recorder instance.
- Not done: `agent_run_steps` implementation and durable tool invocation/
  idempotency writes in production app flows.

```sql
CREATE TABLE agent_runs (
  run_id              TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  session_id          TEXT NOT NULL,
  request_id          TEXT NOT NULL,
  status              TEXT NOT NULL,
  model               TEXT,
  prompt_version      TEXT,
  prompt_hash         TEXT,
  created_at          TIMESTAMPTZ NOT NULL,
  updated_at          TIMESTAMPTZ NOT NULL,
  completed_at        TIMESTAMPTZ,
  error_class         TEXT,
  error_message       TEXT
);

CREATE TABLE agent_run_steps (
  step_id             TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES agent_runs(run_id),
  step_seq            INTEGER NOT NULL,
  step_type           TEXT NOT NULL,
  status              TEXT NOT NULL,
  idempotency_key     TEXT,
  input_hash          TEXT,
  output_ref          TEXT,
  started_at          TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  UNIQUE (run_id, step_seq),
  UNIQUE (run_id, idempotency_key)
);

CREATE TABLE agent_run_events (
  run_id              TEXT NOT NULL REFERENCES agent_runs(run_id),
  event_seq           BIGINT NOT NULL,
  event_type          TEXT NOT NULL,
  payload             JSONB NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (run_id, event_seq)
);

CREATE TABLE agent_tool_invocations (
  invocation_id       TEXT PRIMARY KEY,
  run_id              TEXT NOT NULL REFERENCES agent_runs(run_id),
  step_id             TEXT NOT NULL REFERENCES agent_run_steps(step_id),
  tenant_id           TEXT NOT NULL,
  tool_name           TEXT NOT NULL,
  args_hash           TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  risk_level          TEXT NOT NULL,
  approval_id         TEXT,
  status              TEXT NOT NULL,
  result_ref          TEXT,
  error_class         TEXT,
  error_message       TEXT,
  created_at          TIMESTAMPTZ NOT NULL,
  completed_at        TIMESTAMPTZ,
  UNIQUE (tenant_id, tool_name, idempotency_key)
);
```

## 5) Idempotency Model

Agent runtime idempotency is workflow idempotency, not only API idempotency.

Levels:

1. Request/run idempotency: duplicate client submit should attach to the same
   run, not create a second run.
2. Step idempotency: resume/replay should not repeat a committed step.
3. Tool idempotency: mutating tools must not repeat side effects.
4. Approval idempotency: double-click approval must not execute twice.
5. Event idempotency: reconnect should replay events, not rerun work.

Recommended key strategy:

```text
run_id:
  client-supplied idempotency key if present, otherwise generated server-side

step_id:
  run_id + monotonic step_seq

tool idempotency_key:
  hash(run_id, step_id, tool_name, normalized_args)

approval_id:
  hash(run_id, step_id, tool_invocation_id, policy_version)
```

Parameter mismatch rule:

- Same idempotency key + same args hash: return existing status/result.
- Same idempotency key + different args hash: reject as conflict.
- Existing status `processing`: return retryable in-progress response.
- Existing status `success`: return cached result.
- Existing status `failed`: retry only if policy marks the error retryable.

## 6) Refactor Roadmap

## Phase 0 - Document and freeze current runtime facts

Goal: create a shared map before code movement.

Tasks:

- Keep this document current.
- Link it from `platform/docs/README.md`.
- Add a short "runtime ownership" section to Agent Server docs if needed.

Exit criteria:

- Everyone can answer where run, session, tool, policy, and event state live
  today.

## Phase 1 - Runtime event log without behavior changes

Status: in progress. Completed slices add a process-local `RunRecorder`
contract, ordered `RunEvent` records, chat/runtime emission points, a
SQLite-compatible durable `RuntimeStore`, and a `StoreBackedRunRecorder`
adapter without changing the frontend protocol or LangGraph behavior.

Goal: introduce observability-grade run events while keeping existing graph and
SSE behavior.

Tasks:

- Add lightweight `app/runtime/` module.
- Add `RunRecorder` interface with in-memory implementation first.
- Emit events from `chat.py` at existing points:
  - run_started
  - status_thinking
  - model_call_started/model_call_completed
  - tool_started/tool_completed
  - run_completed/run_failed
- Use existing `run_id`, `request_id`, `session_id`, `tenant_id`.

Exit criteria:

- Tests can reconstruct a run timeline from `RunRecorder`.
- Tests can reconstruct a persisted run timeline from a fresh store instance.
- No change to frontend protocol.

## Phase 2 - Stable SSE event ids and replay-ready stream

Status: in progress. First slice adds stable event ids to status/tool named SSE
events and exposes `GET /v1/runs/{run_id}/events?after_seq=N` for tenant-scoped
timeline inspection. OpenAI-compatible content chunks remain unchanged.

Goal: make the event stream cursor-compatible.

Tasks:

- Add monotonically increasing event sequence per run.
- Include stable SSE `id:` using `run_id:event_seq`.
- Keep old OpenAI chunk format for compatibility.
- Add an internal API/helper to fetch events after a cursor.

Exit criteria:

- Unit test proves duplicate stream consumption does not duplicate runtime work.

## Phase 3 - ToolInvocationEnvelope for tool calls

Status: first enforced slice complete. The envelope/hash/idempotency-key
contract is evaluated by a shared wrapper before every main-agent and subagent
tool operation. Runtime/SSE events observe the decision but no longer control
whether the operation executes.

Goal: route every tool call through one wrapper before execution.

Tasks:

- Define `ToolInvocationEnvelope`.
- Compute normalized args hash.
- Generate idempotency key.
- Attach tenant/request/run/session metadata.
- Emit invocation start/end events.
- Mark mutating tools explicitly in registry metadata.

Exit criteria:

- `file_read` and `memory_search` pass through the envelope.
- At least one mutating local tool, likely `file_write`, passes through the
  envelope in dry-run/log-only mode.

## Phase 4 - Idempotency store for mutating tools

Status: in progress. First slice defines an in-memory idempotency store state
machine (`processing/success/failed`) with conflict, in-progress, success replay,
and controlled failed-retry semantics. `file_write` now uses the store for a
first narrow enforcement path inside active runs; other tools remain log-only.

Goal: prevent repeated side effects on retry/resume.

Tasks:

- Add persistent store for tool invocation status/result.
- Enforce same-key/same-args replay.
- Reject same-key/different-args.
- Return in-progress for concurrent duplicate execution.
- Start with `file_write`, `file_edit`, `git_commit`, `code_shell`.

Exit criteria:

- Test simulates crash/retry around a mutating tool and proves the side effect
  is not repeated.

## Phase 5 - PolicyDecision engine

Status: first enforcement slice complete. `ALLOW` executes,
`REQUIRE_APPROVAL` pauses before the operation, and all other actions fail
closed. Denials and approval decisions are recorded as runtime events.

Goal: centralize scattered runtime policy into a queryable decision.

Tasks:

- Add YAML policy config for tool risk and approval needs.
- Implement policy evaluation before tool execution.
- Emit `policy_decision` event.
- Start with allow/deny/log-only; defer interactive approval until the event
  contract is stable.

Exit criteria:

- `code_shell` and `git_commit` can be marked `require_approval` or `deny` by
  config without changing prompt text.

## Phase 6 - Durable pause/resume

Status: partial. Approval requests and run checkpoints persist in SQLite,
`/v1/runs/{run_id}/resume` uses a strict lease, and terminal checkpoints cannot
regress. An `executing` checkpoint is treated as an unknown outcome and is not
automatically replayed. Same-run approval-gated tools are serialized into
monotonic checkpoint steps.

Goal: support HITL and long-running tasks without holding one HTTP request open.

Tasks:

- Add `Run.status = paused`.
- Persist pending approval objects.
- Add resume endpoint.
- On resume, continue from saved run state and committed step/tool results.

Exit criteria:

- A high-risk tool can pause, process can restart, user approves, run resumes
  without redoing previous committed steps.

## Phase 7 - Domain pack contract

Goal: make coding/customer/health a configuration-and-policy switch, not a fork.

Tasks:

- Define domain pack layout:
  - `agent.yaml`
  - `tools.yaml`
  - `policies.yaml`
  - `retrieval.yaml`
  - `memory.yaml`
  - `eval.yaml`
  - `handoff.yaml`
- Implement a minimal `customer-qa` pack first.
- Keep health as design-only until policy/eval/handoff are mature.

Exit criteria:

- Same runtime can load `coding-agent` and `customer-qa-agent` with different
  tool risk and eval settings.

## 7) Small-Step Implementation Preference

Prefer this order:

1. Add runtime recorder and events.
2. Add stable event ids.
3. Add tool envelope in log-only mode.
4. Add idempotency store for one mutating tool.
5. Add policy decisions.
6. Add approval/pause/resume.

Avoid:

- Big-bang replacement of LangGraph.
- Rewriting all tools at once.
- Moving frontend protocol before event semantics are stable.
- Treating Redis fallback as production-grade durability.

## 8) Integration Testing and Evaluation Plan

Unit tests prove the runtime contracts, but they do not prove the platform works
as a user experiences it. Add an API-level scenario harness that drives the
agent like a human would.

## 8.1 Scenario runner shape

```text
platform/agent-server/scripts/runtime_scenario.py
  -> create/login test tenant
  -> open or prepare a temp workspace
  -> POST /v1/chat/completions with stream=true
  -> collect SSE content + named status/tool events
  -> GET /v1/runs/{run_id}/events
  -> assert final answer, tool trajectory, runtime metadata, and file effects
```

Recommended first scenarios:

1. Read-only coding question:
   - User: "Read README and summarize the project."
   - Expect: `file_list/file_read` or `rag_search`, no write/execute tools.
   - Assert: final answer grounded in README, run events contain model/tool
     timeline, policy decisions are `allow`.
2. Safe file write:
   - User: "Create hello.py that prints hello."
   - Expect: `file_write`, file exists, runtime event has invocation envelope,
     risk=`write`, idempotency metadata present.
3. Duplicate/retry simulation:
   - Repeat the same write step using the same run/idempotency context in a
     controlled test helper.
   - Expect: no second side effect, cached success replay.
4. Dangerous command policy dry run:
   - User: asks for shell execution.
   - In log-only phase, assert `policy_decision` records execute risk. In
     enforcement phase, assert pause/deny/approval depending on config.

## 8.2 Evaluation dimensions

For each scenario, record:

- Final-answer checks: required substrings, forbidden substrings, citations if
  applicable.
- Tool trajectory: expected tools, forbidden tools, max tool calls.
- Runtime contract: ordered events, stable event ids, run status, tenant match.
- Policy/idempotency: risk classification, policy decision, idempotency key,
  replay behavior.
- Side effects: files created/edited, git diff, external API mock calls.
- Budgets: latency, token estimate, tool count.

This should eventually feed `platform/eval-harness/` so smoke scenarios and
runtime contract checks are reported together.

## 8.3 Product workflow and acceptance gates

Every roadmap slice follows the same loop:

1. **Priority**: state the user risk being reduced and why it outranks the
   other unfinished work.
2. **UX standard**: define what a user should see, understand, and safely retry.
3. **Implementation**: make the smallest reversible change that satisfies the
   contract.
4. **Acceptance**: run unit tests plus the relevant API scenario and inspect
   persisted runtime evidence.
5. **Decision**: only mark the slice done when the acceptance gate passes;
   otherwise record the blocker and do not advance the roadmap.

### User experience standards

For every agent run, a user must be able to:

- identify the run and its current state;
- distinguish working, waiting, completed, failed, and cancelled;
- understand whether a failure is retryable and whether retrying can repeat a
  side effect;
- reconnect or refresh without losing the final result or duplicating work;
- see a concise explanation when a tool is denied, paused, or replayed.

For mutating tools, the minimum trust standard is: one user intent produces at
most one committed side effect for the same idempotency key, and a replay
returns the recorded result rather than executing the tool again.

### Acceptance gates by priority

| Priority | User risk | Required evidence before advancing |
|---|---|---|
| P0 | Run appears stuck or silently fails | API scenario proves terminal status, ordered events, failure reason, and stable run id |
| P0 | Retry repeats a write | Duplicate/retry scenario proves one durable invocation and cached success replay |
| P1 | Disconnect loses confidence | Fresh-process query returns the same run timeline and event ids |
| P1 | Dangerous action surprises user | Policy scenario proves deny/pause/approval state is visible before execution |
| P2 | Domain behavior drifts between agents | Same runtime loads coding and customer-QA packs with domain-specific policy/eval cases |

The current priority is P0 reliability: durable `file_write` idempotency and
clear terminal run semantics. HITL pause/resume and domain packs remain blocked
on this gate rather than being implemented in parallel. Once the shared runtime
milestones are complete, `customer-qa-agent` is the primary product acceptance
target; coding-agent scenarios remain regression coverage for side effects.

First script slice:

- `platform/agent-server/scripts/runtime_scenario.py`
- Scenarios: `readme_summary`, `file_write`
- Requires a running local agent server and a model capable of the requested
  tool use.

## 9) Open Questions

1. Should the durable runtime store be Postgres only, or Postgres plus Redis
   stream cache?
2. Should `session_id` continue to embed tenant (`tenant:session`) or should
   tenant be a separate field everywhere?
3. Do we want client-supplied idempotency keys in the OpenAI-compatible API
   extension field?
4. What is the first mutating tool to enforce idempotency on: `file_write`,
   `file_edit`, `git_commit`, or `code_shell`?
5. Should MCP backend services receive the runtime `ToolInvocationEnvelope`, or
   should the Python runtime translate envelope metadata into headers first?

## 10) Production Challenge Coverage

This section captures the external research checkpoint from 2026-07-31 so we do
not lose the rationale behind the roadmap.

## 10.1 What production reviews and writeups test

Production reviews increasingly focus less on framework quickstarts and more on
whether a team can make
non-deterministic tool-using systems observable, recoverable, safe, and
evaluatable.

Common probes:

1. **Loop exit semantics**
   - Why does an agent stop?
   - Why can it loop forever?
   - Is `max_iterations` a normal exit or failure backstop?
   - What happens after malformed tool calls or repeated tool errors?
2. **Tool contracts and tool safety**
   - Which tools can run automatically?
   - Which tools need approval?
   - How are tool inputs validated?
   - How do we prevent shell/refund/write actions from causing damage?
3. **Trajectory observability**
   - Can operators inspect the exact model calls, tool calls, inputs, outputs,
     prompts, retrieval results, and policy decisions behind one answer?
   - Can they answer "why did the agent do that?"
4. **Evaluation beyond final answer**
   - Did the agent call the right tools?
   - Did it avoid forbidden tools?
   - Was the answer grounded?
   - Did latency, token usage, and tool count stay inside budget?
5. **Durable execution**
   - What happens if the process dies halfway through a long run?
   - Can a paused run resume after approval without redoing previous work?
   - Can work move across machines/workers?
6. **Idempotency and side effects**
   - If a mutating tool times out and the run retries, do we double-write,
     double-refund, double-book, or double-commit?
   - Are request-level, step-level, tool-level, approval-level, and event-level
     idempotency handled separately?
7. **Context rot and memory safety**
   - What should be remembered, for how long, and with what confidence?
   - How do stale memory or over-compressed context create bad behavior?
8. **Parallelism, subagents, streaming, and cancellation**
   - Can child agents stream progress?
   - Can the parent cancel wrong-path work?
   - What happens to a long-running tool call when cancellation arrives?
9. **Security and identity governance**
   - Is the agent a first-class identity?
   - Are credentials scoped per tool/task?
   - Is tenant/resource access enforced before retrieval and tool execution?
10. **Domain migration**
   - Can the same runtime support coding, customer QA, ecommerce, and health?
   - Which parts are runtime contracts vs domain policy/tool packs?

## 10.2 Project coverage today

| Challenge | Current coverage | Notes |
|---|---|---|
| Loop exit/error recovery | Partial/strong | `agent_graph/` has explicit state, max steps, consecutive error tracking, hallucination fix, and stable terminal reasons. |
| Tool contracts/safety | Partial | `ToolInvocationEnvelope`, `ToolRisk`, workspace path validation, and a unified fail-closed pre-execution wrapper are enforced. Need durable policy configuration and uniform timeout/retry metadata. |
| Observability | Partial/strong | Model provenance, runtime run events, tool invocation metadata, `/v1/runs/{run_id}/events`. Need durable store and query/report tooling. |
| Evaluation | Partial/strong | Existing eval harness plus API scenario harness. Need integration into CI/eval reports and domain cases. |
| Durable execution | Early | LangGraph checkpointer exists; runtime recorder/idempotency are still in-memory. Need DB-backed runtime store. |
| Idempotency | Early/partial | In-memory idempotency contract exists; `file_write` has first narrow enforcement path. Need persistent tool invocation table and wrapper for all mutating tools. |
| HITL / approval | Partial | Approval is enforced before ToolNode side effects, checkpoint transitions are monotonic, and approved checkpoints can resume once. Need distributed workers, heartbeat, and explicit reconciliation for unknown `executing` outcomes. |
| Context/memory safety | Partial | Compression, RAG, skills catalog, memory tools exist. Need stale-memory policy and eval. |
| Subagents/cancel | Partial/strong | Fleet bus, child streaming, cancel endpoint, subagent verifier. Need durable/cross-process cancel. |
| Security/identity | Partial | JWT tenancy, lineage headers, workspace security. Need agent identity lifecycle and scoped tool credentials. |
| Domain migration | Design only | Domain mapping docs exist; domain pack contract not implemented. |

## 10.3 Priority implication

The current high-leverage track is not another prompt or tool. It is making the
durable runtime store active and side-effect-aware:

1. Done: define `RuntimeStore` interface.
2. Done: implement a SQLite/Postgres-compatible dev store for:
   - `agent_runs`
   - `agent_run_events`
   - `agent_tool_invocations`
3. Done: add a `StoreBackedRunRecorder` while keeping the API contract stable.
4. Done: add app config to opt into the durable store without changing local
   defaults (`AGENT_RUNTIME_STORE_BACKEND=sqlite` and
   `AGENT_RUNTIME_STORE_SQLITE_PATH=...`).
5. Done for the first P0 slice: persist tool invocation/idempotency records for
   `file_write`; targeted tests and the API `file_write` E2E passed with one
   durable successful invocation despite duplicate model tool requests.
6. Done for the first P0 slice: the two-process acceptance run passed with
   process A writing the run and process B reading the same event timeline and
   stable event ids from the shared SQLite DB.

7. Done: expose `terminal_reason` and `retryable` consistently in in-memory and
   SQLite run metadata, including failed and cancelled terminal paths.
8. Next P0 acceptance: add failed-tool and cancelled-run API scenarios before
   moving to HITL approval/pause-resume.

This closes the biggest gap between the project and production agent
engineering requirements: **recoverability and auditability of
multi-step, side-effecting agent runs**.

## 11) Source Material Already in This Repo

- `platform/docs/design/AGENT-SERVER-DESIGN.md`
- `platform/docs/design/ORCHESTRATOR-DESIGN.md`
- `platform/docs/design/PROMPT-GOVERNANCE-PROVENANCE-IMPLEMENTATION.md`
- `platform/docs/design/ADAPTIVE-PLATFORM-BUILD-FOR-CHANGE-PLAN.md`
