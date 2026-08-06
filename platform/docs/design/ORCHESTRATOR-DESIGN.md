# Agent Orchestrator — Design Discussion

> **Status:** RFC (Request for Comments) — discuss before implementing
> **Goal:** Define what the orchestrator IS, what it OWNS, and what it DELEGATES

---

## 1. First Principles: What Problem Are We Solving?

Today we have 3 independent MCP servers. Each works great alone. But when an LLM agent uses all 3 together, nobody is responsible for:

| Problem | Example | Who Owns It Today? |
|---------|---------|-------------------|
| **Which tool to call?** | User asks "find the bug" — is that file_search, memory_search, or code_run? | LLM decides (no platform help) |
| **Token budget** | Agent used 80K tokens on memory_search results, only 20K left for code_run output | Nobody — each tool returns full output |
| **Tool chaining** | `memory_search` → `file_read` → `code_run` → `memory_set` (save result) | LLM does it manually, step by step |
| **Cross-tool context** | Code exec failed — should we search memory for similar past errors? | Nobody |
| **Cost control** | Tenant hit $50/day budget, should we switch to cheaper model? | Nobody |
| **Session state** | What tools were called, what was returned, across a multi-turn conversation? | Nobody (stateless MCP) |

**Key insight:** MCP is stateless request/response. The orchestrator adds **statefulness, intelligence, and governance** on top.

---

## 2. What the Orchestrator Is NOT

Before defining what it IS, let's be clear about what it's NOT:

| It's NOT | Why |
|----------|-----|
| An API Gateway | We don't need Kong/Nginx — Spring Cloud Gateway can do routing, but the orchestrator is above that |
| An LLM | The LLM (GPT-4, Claude) makes decisions; the orchestrator provides **context and constraints** for those decisions |
| A workflow engine | Temporal/Airflow run predefined DAGs; the orchestrator supports **dynamic, LLM-driven** tool selection |
| A replacement for MCP | MCP stays as the tool protocol; the orchestrator is a **client of MCP servers**, not a replacement |

**The orchestrator is a smart MCP client that sits between the LLM and the MCP servers.**

```
LLM (GPT-4/Claude)
    ↕ (prompts + tool calls)
Agent Orchestrator          ← THIS IS WHAT WE'RE DESIGNING
    ↕ (MCP protocol)
┌───────┬───────┬───────┐
│Memory │Search │CodeEx │
│ MCP   │ MCP   │ MCP   │
└───────┴───────┴───────┘
```

---

## 3. Core Responsibilities

### 3.1 Tool Registry & Discovery

```
Orchestrator starts up → connects to all configured MCP servers
→ calls tools/list on each → builds unified tool catalog
→ exposes merged tool list to the LLM
```

**Design questions:**
- Static config (list of MCP server URLs) or dynamic discovery (Nacos/Eureka)?
- How to handle MCP server going down? Circuit breaker per server?
- Tool name conflicts across servers? (e.g., two servers both have `search`)

**My recommendation:** Start with static config. Dynamic discovery is premature optimization — we have 3 servers, not 300.

### 3.2 Session State Manager

```java
public class AgentSession {
    String sessionId;
    String tenantId;
    List<ToolCall> history;        // what was called, in order
    Map<String, Object> context;   // accumulated results
    TokenBudget budget;            // remaining tokens
    Instant createdAt;
    Instant lastActiveAt;
}
```

**Design questions:**
- Where to store sessions? Redis (fast, volatile) or PostgreSQL (durable)?
- Session timeout? 30 min idle? 24h max?
- Should sessions survive server restart?

**My recommendation:** Redis for active sessions, PostgreSQL for audit trail. TTL 30 min in Redis, async persist to PG.

### 3.3 Token Budget Tracker

```
Agent context window: 128K tokens
- System prompt:     ~2K (fixed)
- Conversation:      ~20K (grows)
- Tool results:      ~50K (variable)  ← THIS IS WHAT WE CONTROL
- Available:         ~56K

When tool returns 30K tokens but budget is 10K:
→ Orchestrator summarizes result to fit budget
→ Or asks tool to paginate (file_read with line range)
```

**Design questions:**
- Who counts tokens? Orchestrator (tiktoken library) or delegate to LLM?
- Summarization: call another LLM to summarize, or just truncate?
- Per-tool budget allocation strategy?

**My recommendation for MVP:** Simple truncation (we already have `ResultTruncator`). Summarization is a Phase 2 feature that requires a "summarizer" LLM call, adding latency and cost.

### 3.4 Tool Call Router

The orchestrator intercepts tool calls from the LLM and routes to the right MCP server:

```
LLM says: call memory_search("java patterns")
Orchestrator:
  1. Look up "memory_search" in tool registry → memory-server:8180
  2. Check tenant budget → OK
  3. Set TenantContext from session
  4. Forward call via MCP SSE to memory-server
  5. Receive result
  6. Track tokens used
  7. Update session history
  8. Return result to LLM
```

**Design questions:**
- Synchronous routing (simple) or async with timeout?
- Should orchestrator transform tool parameters? (e.g., auto-inject tenantId)
- Retry policy per tool?

### 3.5 Cost & Quota Governance

```java
public class TenantQuota {
    int maxToolCallsPerDay;
    int maxTokensPerDay;
    BigDecimal maxCostPerDay;      // for LLM calls
    Set<String> allowedTools;       // tool whitelist per tenant
    Set<String> allowedModels;      // model whitelist
}
```

**Design question:** Where do quotas live?
- Config file (simple, restart to change)
- Database (dynamic, admin UI)
- Per-tenant JWT claims (distributed, no DB lookup)

---

## 4. Architecture Options

### Option A: Thin Proxy (Recommended for MVP)

```
┌─────────────────────────────────────────┐
│  Agent Orchestrator (Thin Proxy)        │
│                                         │
│  ┌──────────┐  ┌──────────────────────┐│
│  │ Tool     │  │ Session Manager     ││
│  │ Registry │  │ (Redis)             ││
│  └────┬─────┘  └──────────────────────┘│
│       │                                 │
│  ┌────▼─────────────────────────────┐  │
│  │ MCP Client (SSE connections)     │  │
│  │ → memory-server:8180             │  │
│  │ → filesearch-server:8280         │  │
│  │ → codeexec-server:8380           │  │
│  └──────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

**The orchestrator is itself an MCP server** — the LLM connects to it, and it fans out to backend MCP servers. This means:
- LLM sees ONE MCP server (the orchestrator)
- Orchestrator merges tool lists from all backends
- All tool calls go through the orchestrator (session tracking, budget)

**Pros:** Simple, no new protocol, LLM config doesn't change when backends are added
**Cons:** Single point of failure, adds latency hop

### Option B: Sidecar / Middleware

```
LLM → Orchestrator middleware → memory-server
                              → filesearch-server
                              → codeexec-server
```

The orchestrator intercepts MCP traffic like a middleware/proxy, without being a full MCP server itself.

**Pros:** Lower latency (pass-through), backends can serve directly
**Cons:** Complex interceptor logic, harder to manage session state

### Option C: Agent Framework Integration

Don't build our own orchestrator — integrate with LangChain4j, Spring AI's agent support, or similar.

**Pros:** Mature ecosystem, community support
**Cons:** Less control, may not fit MCP model exactly

**My recommendation: Option A (Thin Proxy MCP Server)** — it's the simplest, fits MCP's design, and our `mcp-common` library makes it easy to build.

---

## 5. Data Model

```sql
-- Agent sessions
CREATE TABLE agent_sessions (
    id              VARCHAR(36) PRIMARY KEY,
    tenant_id       VARCHAR(128) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    last_active_at  TIMESTAMPTZ NOT NULL,
    metadata        JSONB       -- session-level context
);

-- Tool call audit log
CREATE TABLE tool_calls (
    id              VARCHAR(36) PRIMARY KEY,
    session_id      VARCHAR(36) REFERENCES agent_sessions(id),
    tenant_id       VARCHAR(128) NOT NULL,
    tool_name       VARCHAR(256) NOT NULL,
    mcp_server      VARCHAR(256) NOT NULL,
    request_params  JSONB,
    response_preview TEXT,       -- first 500 chars
    tokens_used     INTEGER,
    duration_ms     INTEGER,
    status          VARCHAR(20), -- SUCCESS, ERROR, TIMEOUT
    created_at      TIMESTAMPTZ NOT NULL
);

-- Tenant quotas
CREATE TABLE tenant_quotas (
    tenant_id           VARCHAR(128) PRIMARY KEY,
    max_calls_per_day   INTEGER DEFAULT 1000,
    max_tokens_per_day  INTEGER DEFAULT 1000000,
    allowed_tools       JSONB,   -- ["memory_*", "file_*"]
    allowed_models      JSONB,   -- ["gpt-4o", "claude-sonnet"]
    updated_at          TIMESTAMPTZ
);
```

---

## 6. Key Design Decisions to Discuss

### Decision 1: MCP-in-MCP or REST?

The orchestrator connects to backend MCP servers. Should it use:
- **MCP SSE** (same protocol) — clean, consistent
- **REST/gRPC** — simpler client, but breaks MCP abstraction

**Trade-off:** MCP SSE is event-driven (good for streaming), but adds complexity. REST is simpler but loses the MCP tool type system.

### Decision 2: Stateful or Stateless Orchestrator?

| Approach | Session Storage | Scalability | Complexity |
|----------|----------------|-------------|------------|
| Stateless | None — each call is independent | Easy horizontal scale | Can't track budget/history |
| Sticky sessions | In-memory per instance | Limited — session pinned to instance | Medium |
| Shared state (Redis) | Redis cluster | Full horizontal scale | Higher — distributed state |

**My recommendation:** Shared state in Redis. We already have Redis for memory-server cache. Session data is small (<10KB per session).

### Decision 3: Sync vs Async Tool Calls

Should the orchestrator:
- Wait for each tool call to complete before returning? (simpler)
- Stream partial results as they arrive? (better UX for slow tools like code_run)

**My recommendation:** Sync for MVP. Streaming requires WebSocket/SSE passthrough which adds complexity.

### Decision 4: Should the Orchestrator Know About LLM Models?

Two designs:
- **Orchestrator manages models** — routes to GPT-4/Claude based on task (Model Router built into orchestrator)
- **Orchestrator is model-agnostic** — separate Model Router MCP server

**My recommendation:** Model-agnostic orchestrator + separate Model Router MCP. Keeps single-responsibility. The orchestrator routes TOOLS, not MODELS. The LLM client (VS Code, Claude Desktop) chooses the model.

---

## 7. Failure Modes & Robustness

| Failure | Impact | Mitigation |
|---------|--------|------------|
| Backend MCP server down | Tools from that server unavailable | Circuit breaker, return "tool temporarily unavailable" |
| Redis down | Session state lost | Graceful degradation: work stateless, log warning |
| Orchestrator restart | Active sessions lost (Redis) | Sessions survive in Redis (TTL-based) |
| Tool call timeout | LLM waits forever | Per-tool timeout (configurable), default 30s |
| Token budget exhausted | LLM can't get results | Return budget-exceeded error, suggest smaller query |
| Tenant quota exceeded | Tenant blocked | Return quota error with reset time |

---

## 8. What We DON'T Build Yet

| Feature | Why Defer |
|---------|-----------|
| Model routing | Separate MCP server, not orchestrator's job |
| Tool chaining automation | Let LLM decide sequence; orchestrator just tracks |
| Semantic tool selection | LLM already does this well; we just provide the catalog |
| Multi-agent coordination | No use case yet |
| Prompt engineering layer | Belongs in LLM client, not middleware |

---

## 9. Estimated Scope

| Component | Files | Lines | Days |
|-----------|-------|-------|------|
| Tool Registry + Config | 3 | ~150 | 0.5 |
| MCP Client (connect to backends) | 3 | ~200 | 1 |
| Session Manager (Redis) | 3 | ~200 | 1 |
| Token Budget Tracker | 2 | ~100 | 0.5 |
| Tool Call Router | 3 | ~250 | 1 |
| Quota Service | 2 | ~150 | 0.5 |
| Orchestrator MCP Server (@Tool) | 1 | ~100 | 0.5 |
| Tests | 5 | ~400 | 1 |
| **Total** | **~22** | **~1550** | **~6 days** |

---

## 10. Open Questions for Discussion

1. **Should we skip the Model Router MCP and fold model routing into the orchestrator?** It simplifies the architecture but violates single-responsibility.

2. **How does tenant auth flow through?** LLM connects to orchestrator with JWT → orchestrator forwards calls to backends. Do backends verify JWT again, or trust the orchestrator's `X-Tenant-Id` header?

3. **MCP SSE multiplexing:** The orchestrator maintains N SSE connections to N backends. Should these be persistent (connection pool) or per-request?

4. **Testing strategy:** How do we integration-test the orchestrator? Mock MCP servers? Or start real backends in Docker?

5. **Is this the right time?** We have 3 backend services and could build the 4th (Model Router) first. Or we could build the orchestrator now and prove the architecture with the 3 existing backends.
