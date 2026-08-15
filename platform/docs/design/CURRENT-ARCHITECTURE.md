# Current Platform Architecture

> Updated 2026-08-15. This is the source of truth for the implemented local stack.

## System View

```mermaid
flowchart LR
  U[User] --> FE[React frontend\nagent switcher + chat + IDE]
  FE -->|GET /api/agents| API[Agent Server]
  FE -->|POST /v1/chat/completions| API
  API --> RUN[Runtime shell\nRunCoordinator]
  RUN --> ENG[AgentEngine]
  ENG --> CFG[Engine registry/cache\nYAML + tenant + prompt]
  CFG --> GRAPH[Modular agent_graph]
  GRAPH -.-> CAP[Capability packages\npolicy / routing / compressor\nmodel / skills / delegation]
  GRAPH --> LLM[Ollama / model gateway]
  GRAPH --> TOOLS[Tool registry]
  TOOLS --> MCP[MCP services\nmemory / filesearch / codeexec]
  TOOLS --> RAG[RAG search tool]
  RAG --> RET[Hybrid retriever\nvector + BM25 + RRF]
  RET --> CE[Optional cross-encoder reranker]
  RET --> IDX[Per-tenant index\nNumPy disk or Redis HNSW]
  IDX --> EMB[Ollama embeddings]
  RUN --> RT[Runtime control plane]
  RUN --> OBS[Observers\nusage / provenance / Kafka / RAG feedback]
  RT --> EVENTS[Durable event log]
  RT --> APPROVAL[Approval store + policy gate]
  RT --> CP[LangGraph checkpoint]
  EVENTS --> SQLITE[(SQLite dev)]
  APPROVAL --> SQLITE
  CP --> SQLITE
```

### Portable ASCII View

```text
Browser -> React -> FastAPI protocol adapters
         |
         v
      Runtime shell: RunCoordinator
    runs/events | policy/approval | recovery | observers
         |
         v
    AgentEngine -> registry/cache -> modular agent_graph
               |
   execution_policy | tool_routing | compressor | model
         skills | delegation
               |
         model providers + authorized tools
               |
        local / RAG / Java MCP backends
               |
     SQLite dev store / Redis / Postgres / Kafka

       RAG tool path from LangGraph:

       query -> vector search + BM25 -> RRF -> cross-encoder -> evidence answer
                         |                         |
                  NumPy disk or Redis HNSW     Ollama embeddings
```

## Implemented Agent Boundary

### Reliability and Production Boundary

The current local runtime uses `SQLiteRuntimeStore`. Its guarantees are
durability and cross-process recovery, but not multi-instance ownership or a
distributed work queue. The production target is the Postgres schema in
`platform/agent-server/migrations/001_runtime_postgres.sql`, paired with a
worker queue and lease-based invocation claims. The capability boundary is
explicit in `app/runtime/backend.py`; SQLite must not be described as a
production horizontal-scaling solution.

### Quality Control Plane

Evaluation artifacts carry `agent_id` and `run_type` (`smoke`, `full`, or
`qa`). Retrieval feedback can produce a candidate reranker artifact, but
promotion and rollback are separate operations. Tool-selection and answer
quality failures remain policy/eval inputs until a candidate passes its gate.

### Weak-Model Tool Contract

For high-confidence explicit intents such as “run Python” or “remember this”,
`app/agent/tool_routing/required_tools.py` detects a required tool. If the model returns a
plain answer without that tool, the graph performs one bounded structured-call
retry and names the missing tool. Ambiguous requests still fall through to the
model; this is a control-plane guard, not an unconditional keyword router.

Agents are resolved from `platform/agent-server/agents/*.yaml`. The frontend
catalog is server-driven by `GET /api/agents`; prompts, tools, guardrails, and
routing remain server-side.

| Agent | Tool posture | Intended use |
|---|---|---|
| `coding-agent` | filesystem, code execution, git, RAG | coding workspace |
| `customer-qa-agent` | read-only RAG and memory search | grounded support FAQ |
| `ops-agent` | investigation and operational tools | incident workflows |

The frontend keeps a separate session id and local transcript per agent. A
switch does not merge graph context across agents.

## Request and Recovery Flow

```mermaid
sequenceDiagram
  participant UI as Frontend
  participant API as Chat API
  participant R as RunCoordinator
  participant E as AgentEngine
  participant G as LangGraph
  participant S as Runtime store
  participant T as Tool
  UI->>API: chat(model, session_id, stream=true)
  API->>R: RuntimeTurnRequest
  R->>S: start run + ordered event
  R->>E: stream turn
  E->>G: resolve cached graph + invoke
  G->>T: execute tool
  T-->>G: result or approval_required
  G-->>E: engine events
  E-->>R: engine event stream
  R->>S: run/tool/model events
  R-->>API: RuntimeStreamEvent
  API-->>UI: OpenAI SSE projection
  UI-->>API: approve or resume
  API->>R: resume request
  R->>S: claim lease + recover checkpoint
  R->>E: resume pending graph node
  R-->>API: completed runtime event
  API-->>UI: final SSE
```

### Portable Recovery Flow

```text
  Browser      Chat API      RunCoordinator      AgentEngine       Runtime Store
     |             |               |                  |                 |
     |-- chat ---->|               |                  |                 |
     |             |-- request --->|-- start run --------------------->|
     |             |               |-- stream turn -->|                 |
     |             |               |<-- engine events-|                 |
     |             |               |-- ordered events ---------------->|
     |<-- SSE ------|<-- runtime ---|                  |                 |
     |-- resume --->|-------------->|-- claim lease ------------------->|
     |             |               |-- resume graph ->|                 |
     |<-- final SSE-|<--------------|<-- completed ----|                 |
```

The event log is the runtime observability surface. Optional observers consume
runtime observations without owning execution outcomes. LangGraph checkpoint owns
execution state; conversation history and long-term memory remain separate
stores with different ownership. SQLite is the development durability path.

## Customer QA RAG Flow

The demo corpus is under `platform/agent-server/knowledge/customer-qa/` and is
indexed per tenant and collection:

```text
public source -> markdown -> section chunk -> mxbai embedding
             -> embeddings.npy/chunks.json
             -> vector + BM25 + RRF -> cross-encoder -> grounded response
```

Redis Stack + RediSearch HNSW is an available production-oriented backend, but
the default local backend remains NumPy memory/disk. The index must eventually
carry embedding model/version, dimension, source URL, document version, update
time, tenant, and ACL metadata.

## Known Gaps

- Agent catalog is read-only; no admin UI or live editing yet.
- Customer QA feedback and retrieval traces are planned in M16.
- Index manifests and freshness/ACL filters are not complete.
- The frontend does not yet expose a full evidence panel or citation inspector.
- Redis is configured as an option, not the default acceptance backend.

## Model Adaptability

The runtime follows the model-adaptive design in
[Model-Adaptive Runtime Design](MODEL-ADAPTIVE-RUNTIME-DESIGN.md): safety
boundaries are invariant, while workflow strictness, tool exposure, context
budgets, planning freedom, and verifier frequency are profile-dependent.

See [Customer QA RAG Quality Design](CUSTOMER-QA-RAG-QUALITY-DESIGN.md).
