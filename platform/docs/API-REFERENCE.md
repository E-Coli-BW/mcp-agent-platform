# API Reference — AI Coding Agent Platform

## Overview

| Service | Port | Base URL | Description |
|---------|------|----------|-------------|
| Agent Server (Python) | 8500 | `http://localhost:8500` | main entry, OpenAI-compatible API + admin API |
| Auth Service (Java) | 8090 | `http://localhost:8090` | RS256 JWT authentication center |
| Memory Server (Java) | 8180 | `http://localhost:8180` | persistent memory storage + TF-IDF search |
| Code Exec Server (Java) | 8380 | `http://localhost:8380` | Docker-sandboxed code execution |

---

## Scenario Classification

### 🔐 Scene 1: Authentication & Session Management

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/auth/signup` | Auth | user signup |
| POST | `/auth/login` | Auth | user login (returns JWT + refresh_token) |
| POST | `/auth/token` | Auth | OAuth2 token（client_credentials / password grant） |
| POST | `/auth/refresh` | Auth | refresh access_token |
| POST | `/auth/logout` | Auth | logout (blacklist the token) |
| POST | `/auth/register` | Auth | register M2M client (requires ADMIN) |
| GET | `/auth/jwks` | Auth | JWKS public-key endpoint (inter-service verification) |
| GET | `/auth/check-blacklist?jti=xxx` | Auth | check whether a token is revoked |
| GET | `/auth/health` | Auth | health check |

### 💬 Scene 2: AI Chat (core business flow)

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/v1/chat/completions` | Agent | OpenAI-compatible chat (SSE streaming) |
| GET | `/v1/models` | Agent | list available models |
| POST | `/v1/sessions/{id}/children/{child}/cancel` | Agent | cancel child agent |

### 🧠 Scene 3: Memory Management (MCP Tools)

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/memory_set` | Memory | write memory |
| POST | `/api/tools/memory_get` | Memory | read memory |
| POST | `/api/tools/memory_search` | Memory | TF-IDF memory search |
| POST | `/api/tools/memory_delete` | Memory | delete memory |
| POST | `/api/tools/memory_list` | Memory | list all memories |
| POST | `/api/tools/memory_pin` | Memory | pin / unpin |
| POST | `/api/tools/memory_context` | Memory | memory system overview |

### 🎓 Scene 4: Skill Storage (Skill Store)

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/skill_set` | Memory | save skill |
| POST | `/api/tools/skill_get` | Memory | get skill |
| POST | `/api/tools/skill_list` | Memory | list skills |
| POST | `/api/tools/skill_history` | Memory | skill version history |
| POST | `/api/tools/skill_rollback` | Memory | roll back skill version |
| POST | `/api/tools/skill_feedback` | Memory | skill usage feedback |
| GET | `/api/tools/skill_triggers` | Memory | skill trigger rules |

### 🖥️ Scene 5: Code Execution

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/code_run` | CodeExec | run code (Docker-isolated) |
| POST | `/api/tools/code_shell` | CodeExec | execute shell command |

### 📁 Scene 6: Workspace Management

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| GET | `/api/workspace/current` | Agent | current workspace path |
| POST | `/api/workspace/open` | Agent | open workspace |
| GET | `/api/workspace/browse?path=xxx` | Agent | browse directory |
| GET | `/api/workspace/files` | Agent | list files |
| GET | `/api/workspace/file?path=xxx` | Agent | read file content |

### 📊 Scene 7: Operations & Observability

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| GET | `/health` | Agent | health check |
| GET | `/metrics` | Agent | request metrics (QPS, latency) |
| GET | `/api/usage` | Agent | token usage & cost |
| GET | `/api/prompts` | Agent | prompt versions & canary config |
| GET | `/api/reranker` | Agent | reranker weight info |
| POST | `/api/reranker/retrain` | Agent | trigger reranker retraining |
| GET | `/api/reranker/evaluate` | Agent | evaluate reranker effectiveness |
| GET | `/api/analytics` | Memory | event analytics |

---

## Sequence Diagrams

### Full Conversation Flow

```
┌──────┐       ┌──────────┐       ┌────────┐       ┌──────────┐       ┌──────────┐
│Client│       │Agent Svc │       │Auth Svc│       │Memory Svc│       │CodeExec  │
└──┬───┘       └────┬─────┘       └───┬────┘       └────┬─────┘       └────┬─────┘
   │                │                  │                  │                  │
   │ POST /auth/login                  │                  │                  │
   │────────────────────────────────► │                  │                  │
   │ ◄─── {access_token, refresh_token}│                  │                  │
   │                │                  │                  │                  │
   │ POST /v1/chat/completions         │                  │                  │
   │ [Authorization: Bearer xxx]       │                  │                  │
   │───────────────►│                  │                  │                  │
   │                │ verify JWT (JWKS)│                  │                  │
   │                │─────────────────►│                  │                  │
   │                │◄─── valid        │                  │                  │
   │                │                  │                  │                  │
   │                │ ReAct Loop ──────────────────────────────────────────  │
   │                │ │                │                  │                  │
   │ ◄── SSE: thinking                │                  │                  │
   │                │ │ LLM decides: use memory_search   │                  │
   │ ◄── SSE: tool_start              │                  │                  │
   │                │ │────────────────────────────────►  │                  │
   │                │ │◄─── search results               │                  │
   │ ◄── SSE: tool_end                │                  │                  │
   │                │ │                │                  │                  │
   │                │ │ LLM decides: use code_run         │                  │
   │ ◄── SSE: tool_start              │                  │                  │
   │                │ │───────────────────────────────────────────────────► │
   │                │ │◄─── execution result             │                  │
   │ ◄── SSE: tool_end                │                  │                  │
   │                │ │                │                  │                  │
   │                │ │ LLM generates final answer        │                  │
   │ ◄── SSE: content tokens (streamed)│                  │                  │
   │ ◄── SSE: [DONE]                  │                  │                  │
   │                │                  │                  │                  │
```

### Authentication Flow Swimlane

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Authentication Flow                            │
├──────────┬───────────────────────┬──────────────────────────────────┤
│  Client  │     Agent Server      │          Auth Service             │
├──────────┼───────────────────────┼──────────────────────────────────┤
│          │                       │                                    │
│ signup ──────────────────────────────► POST /auth/signup              │
│          │                       │  ◄── {user_id, tenant_id}         │
│          │                       │                                    │
│ login ───────────────────────────────► POST /auth/login              │
│          │                       │  ◄── {access_token (15min),       │
│          │                       │       refresh_token (7d)}         │
│          │                       │                                    │
│ chat ───►│ JWT middleware        │                                    │
│          │ ├─ decode token       │                                    │
│          │ ├─ verify signature ──────► GET /auth/jwks (cached)       │
│          │ ├─ check expiry       │                                    │
│          │ ├─ extract tenant_id  │                                    │
│          │ └─ set request.state  │                                    │
│          │ [proceed to handler]  │                                    │
│          │                       │                                    │
│ refresh ─────────────────────────────► POST /auth/refresh            │
│          │                       │  ◄── {new access_token}           │
│          │                       │                                    │
│ logout ──────────────────────────────► POST /auth/logout             │
│          │                       │  (token blacklisted)              │
└──────────┴───────────────────────┴──────────────────────────────────┘
```

### Multi-tenant Isolation Swimlane

```
┌────────────────────────────────────────────────────────────────────┐
│                    Multi-Tenancy Isolation                           │
├──────────┬───────────────────────┬─────────────────────────────────┤
│ Request  │     Middleware Stack   │         Data Layer               │
├──────────┼───────────────────────┼─────────────────────────────────┤
│          │                       │                                   │
│ HTTP ───►│ 1. CORS check         │                                   │
│          │ 2. JWT decode ────────────► tenant_id = jwt["tenant_id"]  │
│          │ 3. Rate limit (IP)    │                                   │
│          │ 4. Rate limit (tenant)│    ← lookup RPM for tenant       │
│          │ 5. Set context ───────────► TenantContext.set(tenant_id)  │
│          │                       │                                   │
│          │ Handler executes      │                                   │
│          │ ├─ Redis key: ────────────► conv:{tenant}:{session}       │
│          │ ├─ Memory API: ───────────► header X-Tenant-Id            │
│          │ ├─ LangGraph thread: ─────► thread_id = {tenant}:{sid}    │
│          │ └─ Kafka event: ──────────► event.tenant_id = tenant      │
│          │                       │                                   │
└──────────┴───────────────────────┴─────────────────────────────────┘
```
