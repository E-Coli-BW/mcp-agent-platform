# Coding Agent Server — Design Document

> **Status:** Design → implement  
> **Language:** Python (FastAPI + LangGraph)  
> **Connects to:** Java MCP tool backends (memory:8180, filesearch:8280, codeexec:8380)  
> **Goal:** OpenAI-compatible coding agent with RAG, streaming, and tool use

---

## 1. What We're Building

A coding agent server that a developer connects to via any OpenAI-compatible client (Open WebUI, LobeChat, Continue.dev, or curl). The agent can search code, read files, execute code, remember context across sessions, and explain/generate code.

```
Developer (IDE / Chat UI / curl)
    ↓ OpenAI-compatible API (POST /v1/chat/completions)
    ↓ SSE streaming
┌─────────────────────────────────────────────────┐
│           Coding Agent Server (Python)           │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │  /v1/chat/completions (OpenAI-compatible)  │ │
│  │  /v1/models (list available models)        │ │
│  └──────────────┬─────────────────────────────┘ │
│                 │                                │
│  ┌──────────────▼─────────────────────────────┐ │
│  │  Agent Loop (LangGraph ReAct)              │ │
│  │  1. Classify intent                        │ │
│  │  2. Retrieve context (RAG)                 │ │
│  │  3. Select tools                           │ │
│  │  4. Execute tools (→ Java MCP backends)    │ │
│  │  5. Synthesize response                    │ │
│  │  6. Stream to client                       │ │
│  └──────────────┬─────────────────────────────┘ │
│                 │                                │
│  ┌──────────────▼─────────────────────────────┐ │
│  │  Supporting Services                       │ │
│  │  ├── Model Router (which LLM?)             │ │
│  │  ├── RAG Pipeline (vector + BM25 hybrid)   │ │
│  │  ├── Conversation Store (Redis)            │ │
│  │  ├── Response Cache (semantic dedup)       │ │
│  │  ├── Token Budget Tracker                  │ │
│  │  └── Cost Accounting (per-tenant)          │ │
│  └──────────────┬─────────────────────────────┘ │
│                 │                                │
│  ┌──────────────▼─────────────────────────────┐ │
│  │  Tool Clients (HTTP → Java MCP backends)   │ │
│  │  ├── memory-server:8180                    │ │
│  │  ├── filesearch-server:8280                │ │
│  │  ├── codeexec-server:8380                  │ │
│  │  └── (model-router:8480 OR embedded)       │ │
│  └────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

---

## 2. Core Components

### 2.1 API Layer — OpenAI-Compatible

We implement the [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat/create) so any compatible client works:

```python
POST /v1/chat/completions
{
  "model": "coding-agent",        # our agent, not a raw LLM
  "messages": [
    {"role": "system", "content": "You are a coding assistant."},
    {"role": "user", "content": "Fix the login bug in auth.py"}
  ],
  "stream": true,                 # SSE streaming
  "temperature": 0.7
}

Response (streamed):
data: {"choices": [{"delta": {"content": "Let me search"}}]}
data: {"choices": [{"delta": {"content": " for the login code..."}}]}
...
data: {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}
data: [DONE]
```

**Why OpenAI-compatible?**
- Works with Open WebUI, LobeChat, Continue.dev out of the box
- No custom frontend needed for MVP demo
- Standard that every AI engineer understands

### 2.2 Agent Loop — LangGraph ReAct

```
User message
    ↓
┌─ Intent Classifier (cheap LLM call) ──────────────────────┐
│  Input: user message + conversation history                │
│  Output: {intent, plan: [{tool, params, reason}]}          │
│  Cost: ~100 tokens, ~200ms                                 │
└────────────────────────────────────────────────────────────┘
    ↓
┌─ RAG Retrieval (if code context needed) ──────────────────┐
│  1. Embed query → vector search (pgvector)                │
│  2. BM25 keyword search (parallel)                        │
│  3. Merge + re-rank (cross-encoder)                       │
│  4. Metadata filter (language, recency, proximity)        │
│  5. Return top-K chunks with source attribution           │
└────────────────────────────────────────────────────────────┘
    ↓
┌─ Tool Execution (iterate until done) ─────────────────────┐
│  For each tool in plan:                                    │
│    1. Call Java MCP backend via HTTP                       │
│    2. Observe result                                       │
│    3. Decide: need more tools? or ready to respond?        │
│  Max iterations: 5 (prevent infinite loops)                │
└────────────────────────────────────────────────────────────┘
    ↓
┌─ Response Synthesis + Stream ─────────────────────────────┐
│  Compose final answer from:                                │
│    - Retrieved context                                     │
│    - Tool results                                          │
│    - LLM reasoning                                         │
│  Stream token-by-token via SSE                             │
│  Auto-save to memory if significant (memory_set)           │
└────────────────────────────────────────────────────────────┘
```

### 2.3 RAG Pipeline

```
Code Ingestion (offline):
  1. Scan codebase → tree-sitter AST parsing
  2. Chunk by code structure (function, class, method)
  3. Attach metadata: file_path, language, last_modified, dependencies
  4. Embed chunks → store in pgvector
  5. Index keywords → store in PostgreSQL (BM25 via ts_vector)

Query Time (online):
  1. User query → embed
  2. Vector search: top 50 by cosine similarity
  3. BM25 search: top 50 by keyword match
  4. Reciprocal Rank Fusion: merge both result sets
  5. Cross-encoder re-rank: top 50 → top 10
  6. Metadata filter: language match, recency boost
  7. Return top 10 chunks with source file + line numbers
```

**Key decisions:**
- **Chunking:** tree-sitter AST (function/class boundaries), NOT fixed-size
- **Embedding:** `nomic-embed-text` via Ollama (free, local, good for code)
- **Vector DB:** pgvector (PostgreSQL extension — reuse our existing PG)
- **Re-ranking:** `BAAI/bge-reranker-base` (local, fast)
- **Hybrid search:** Vector + BM25 merged via Reciprocal Rank Fusion

### 2.4 SSE Streaming (Without Duplication)

The availability-evaluation problem you mentioned. Root causes and fixes:

```
Problem 1: Client reconnects without last-event-id
Fix: Include event IDs in SSE stream
  data: {"id": "evt_001", "choices": [...]}
  data: {"id": "evt_002", "choices": [...]}
  Client reconnects with Last-Event-ID: evt_002
  Server resumes from evt_003

Problem 2: Server buffers + client reads overlap
Fix: Sequence numbers + client-side dedup
  Each chunk has monotonic sequence number
  Client tracks last seen sequence, skips duplicates

Problem 3: Multiple SSE connections for same session
Fix: Session locking — one active stream per session_id
  If new connection arrives for same session, close the old one

Implementation:
  async def stream_response(session_id: str):
      seen = set()
      async for event in agent.astream(messages):
          event_id = f"{session_id}_{sequence}"
          if event_id in seen:
              continue  # dedup
          seen.add(event_id)
          yield f"id: {event_id}\ndata: {json.dumps(event)}\n\n"
          sequence += 1
```

### 2.5 Model Router (Embedded)

Reuse the `LlmProvider` SPI concept from our Java Model Router, but in Python:

```python
class ModelRouter:
    """Route to best model based on task type and cost."""
    
    def route(self, task_type: str, budget: TokenBudget) -> LLMConfig:
        if task_type == "classify" or task_type == "summarize":
            return self.cheap_model    # GPT-4o-mini or local Ollama
        if task_type == "code_generate":
            return self.strong_model   # Claude Sonnet or GPT-4o
        if task_type == "explain":
            return self.medium_model   # whatever's available
        return self.default_model
    
    # Fallback chain: primary → secondary → local
    providers = [OpenAIProvider, AnthropicProvider, OllamaProvider]
```

### 2.6 Conversation Store

```python
# Redis for active conversations, PostgreSQL for history
class ConversationStore:
    async def get(self, session_id: str) -> Conversation:
        # Try Redis first (TTL 30 min)
        # Fall back to PostgreSQL
    
    async def append(self, session_id: str, message: Message):
        # Write to Redis (immediate)
        # Async write to PostgreSQL (durable)
    
    async def get_context_window(self, session_id: str, max_tokens: int) -> list[Message]:
        # Return recent messages that fit in token budget
        # Summarize old messages if needed
```

### 2.7 Retrieval Quality Monitor

```python
# Log every retrieval for offline analysis
class RetrievalLogger:
    async def log(self, query: str, chunks: list[Chunk], 
                  chunks_used_by_agent: list[str], user_feedback: str = None):
        await db.insert("retrieval_logs", {
            "query": query,
            "chunks_returned": [c.id for c in chunks],
            "chunks_used": chunks_used_by_agent,
            "feedback": user_feedback,
            "timestamp": datetime.now()
        })

# Offline eval against gold standard
class RetrievalEvaluator:
    def evaluate(self, gold_standard: list[QueryDocPair]) -> Metrics:
        results = []
        for qd in gold_standard:
            retrieved = rag.search(qd.query)
            results.append({
                "recall@10": recall_at_k(qd.expected_docs, retrieved, k=10),
                "mrr": mean_reciprocal_rank(qd.expected_docs, retrieved),
            })
        return aggregate(results)
```

---

## 3. Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Web framework | FastAPI | Async, SSE support, OpenAPI docs, industry standard |
| Agent framework | LangGraph | Stateful agent loops, better than raw LangChain |
| LLM client | `litellm` | Unified API for OpenAI/Anthropic/Ollama, retries built-in |
| Embedding | `nomic-embed-text` via Ollama | Free, local, good for code |
| Vector DB | pgvector (PostgreSQL extension) | Reuse existing PG, no new infra |
| Keyword search | PostgreSQL `tsvector` | BM25 equivalent, already in PG |
| Re-ranker | `BAAI/bge-reranker-base` via `sentence-transformers` | Local, fast, high quality |
| Code parsing | `tree-sitter` | AST-based chunking for any language |
| Tokenizer | `tiktoken` | Accurate token counting for OpenAI models |
| Conversation store | Redis + PostgreSQL | Fast access + durable history |
| Streaming | FastAPI `StreamingResponse` | Native SSE, async generators |
| Testing | `pytest` + `pytest-asyncio` | Async test support |

---

## 4. Project Structure

```
platform/agent-server/
├── pyproject.toml                    # Dependencies (poetry/uv)
├── app/
│   ├── main.py                      # FastAPI app, routes
│   ├── api/
│   │   ├── chat.py                  # POST /v1/chat/completions
│   │   ├── models.py                # GET /v1/models
│   │   └── health.py                # GET /health
│   ├── agent/
│   │   ├── graph.py                 # LangGraph ReAct agent definition
│   │   ├── intent.py                # Intent classifier
│   │   ├── planner.py               # Tool execution planner
│   │   └── prompts.py               # System prompts, templates
│   ├── rag/
│   │   ├── chunker.py               # tree-sitter code chunking
│   │   ├── embedder.py              # Embedding service
│   │   ├── retriever.py             # Hybrid search (vector + BM25)
│   │   ├── reranker.py              # Cross-encoder re-ranking
│   │   └── evaluator.py             # Retrieval quality metrics
│   ├── llm/
│   │   ├── router.py                # Model selection
│   │   ├── providers.py             # OpenAI, Anthropic, Ollama adapters
│   │   └── cache.py                 # Semantic response cache
│   ├── tools/
│   │   ├── mcp_client.py            # HTTP client for Java MCP backends
│   │   ├── memory.py                # memory_search, memory_set wrappers
│   │   ├── filesearch.py            # file_search, file_read wrappers
│   │   └── codeexec.py              # code_run wrapper
│   ├── streaming/
│   │   ├── sse.py                   # SSE formatter with event IDs
│   │   └── dedup.py                 # Deduplication logic
│   ├── store/
│   │   ├── conversation.py          # Redis + PG conversation store
│   │   ├── token_budget.py          # Token tracking per session
│   │   └── cost.py                  # Per-tenant cost accounting
│   └── config.py                    # Settings from env/yaml
├── tests/
│   ├── test_agent.py                # Agent loop tests
│   ├── test_rag.py                  # RAG pipeline tests
│   ├── test_streaming.py            # SSE streaming tests
│   ├── test_tools.py                # MCP tool client tests
│   └── test_router.py               # Model routing tests
└── docker-compose.yml               # PG + Redis + Ollama
```

---

## 5. Implementation Order

```
Day 1-2: Foundation
  ├── FastAPI app skeleton
  ├── OpenAI-compatible /v1/chat/completions endpoint
  ├── SSE streaming with event IDs + dedup
  ├── MCP tool client (HTTP → Java backends)
  └── Tests: streaming, tool client

Day 3-4: Agent Loop
  ├── LangGraph ReAct agent
  ├── Intent classifier (LLM-as-classifier)
  ├── Tool wrappers (memory, filesearch, codeexec)
  ├── Conversation store (Redis)
  └── Tests: agent loop, intent classification

Day 5-6: RAG Pipeline
  ├── tree-sitter code chunker
  ├── Embedding service (Ollama nomic-embed-text)
  ├── pgvector setup + hybrid search
  ├── Cross-encoder re-ranker
  └── Tests: chunking, retrieval quality

Day 7: Polish
  ├── Model router (task-based selection)
  ├── Response cache (semantic dedup)
  ├── Cost accounting
  ├── Retrieval quality monitor
  └── Integration test: full agent flow
```

---

## 6. Key Design Decisions

| Decision | Choice | Alternative | Why |
|----------|--------|-------------|-----|
| Language | Python | Java | LLM ecosystem is Python-first |
| Agent framework | LangGraph | Raw loops, CrewAI | Stateful graphs, production-ready, good docs |
| LLM client | litellm | openai SDK directly | Unified API across providers, retry built-in |
| Vector DB | pgvector | Milvus, ChromaDB, Pinecone | Reuse existing PG, no new infra to manage |
| Embedding | Ollama local | OpenAI API | Free, no API key needed for demo |
| Chunking | tree-sitter AST | fixed-size | Code structure preserved, much better retrieval |
| Search | Hybrid (vector + BM25) | Vector-only | BM25 catches exact keywords that vectors miss |
| Streaming | SSE with event IDs | WebSocket | OpenAI API compatibility, simpler client |
| Conversation store | Redis + PG | Redis-only | Durable history for audit + analysis |

---

## 7. Technical Highlights

| Feature | Technical Signal |
|---------|-----------------|
| OpenAI-compatible API | "I understand the industry standard LLM API" |
| ReAct agent loop | "I built autonomous agents, not just prompt wrappers" |
| SSE streaming with dedup | "I solved real production streaming issues" |
| Hybrid RAG (vector + BM25) | "I know why vector-only search isn't enough" |
| tree-sitter code chunking | "I understand that code needs semantic chunking, not fixed-size" |
| Cross-encoder re-ranking | "I know the retrieval pipeline: recall → precision" |
| Retrieval quality monitoring | "I can measure and improve RAG quality" |
| Model routing by task type | "I optimize for cost/quality tradeoff" |
| Tool use via MCP backends | "I integrate AI agents with real backend services" |
| Polyglot (Python agent + Java backends) | "I choose the right tool for each job" |

---

## 8. Demo Scenario

```
User: "Fix the authentication bug that was reported yesterday"

Agent (internally):
  1. Intent: code_fix (confidence: 0.9)
  2. Plan: memory_search → file_search → code_run → respond
  
  Step 1: memory_search("authentication bug yesterday")
  → Found: "Bug #42: JWT token not refreshing on session timeout"
  
  Step 2: file_search("JWT refresh token", directory="src/auth")  
  → Found: JwtAuthFilter.java:47 — missing refresh logic
  
  Step 3: RAG retrieval for "JWT refresh token pattern in Spring"
  → Retrieved: 3 relevant code chunks showing refresh patterns
  
  Step 4: LLM generates fix based on context
  → Produces: code diff for JwtAuthFilter.java
  
  Step 5: code_run("python -c 'print(\"fix looks syntactically valid\")'")
  → Validation passed

Agent (to user, streamed):
  "I found the bug. In `JwtAuthFilter.java:47`, the token refresh
  logic is missing. Based on the project patterns, here's the fix:
  
  ```java
  // Add after line 47
  if (isTokenExpired(claims) && hasRefreshToken(request)) {
      token = refreshToken(claims.getSubject());
      response.setHeader("X-New-Token", token);
  }
  ```
  
  I've saved this fix to memory for future reference."
  
  Step 6: memory_set("auth-bug-fix-42", "JWT refresh fix: ...")
```
