# MCP Agent Platform

A full-stack **Agent runtime platform** for building, deploying, and evaluating LLM-powered coding agents with distributed tool backends.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (React)                      │
├─────────────────────────────────────────────────────────┤
│              Python Agent Server (FastAPI + LangGraph)    │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│   │  ReAct   │ │  Intent  │ │ Reflexion│ │ Sub-agent│  │
│   │  Agent   │ │  Router  │ │  Loop    │ │ Orchestr │  │
│   └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
├─────────────────────────────────────────────────────────┤
│              Java Microservices (Spring Boot 3)           │
│   ┌──────┐ ┌────────┐ ┌──────────┐ ┌───────┐ ┌──────┐ │
│   │ Auth │ │ Memory │ │ Code Exec│ │Model  │ │File  │ │
│   │Service│ │ Server │ │  Server  │ │Router │ │Search│ │
│   └──────┘ └────────┘ └──────────┘ └───────┘ └──────┘ │
├─────────────────────────────────────────────────────────┤
│         TypeScript MCP Servers (stdio protocol)          │
│   ┌──────────────┐  ┌──────────────────┐               │
│   │ memory-store │  │ local-file-search │               │
│   └──────────────┘  └──────────────────┘               │
├─────────────────────────────────────────────────────────┤
│   PostgreSQL  │  Redis  │  Kafka  │  Docker (sandbox)   │
└─────────────────────────────────────────────────────────┘
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent Runtime | Python 3.11+ / FastAPI / LangGraph / LangChain |
| Backend Services | Java 21 / Spring Boot 3.4 / JPA / MyBatis-Plus |
| MCP Servers | TypeScript / Node.js |
| Frontend | React 19 / Vite / TypeScript |
| Database | PostgreSQL (tsvector + GIN) / Redis |
| Messaging | RabbitMQ / Kafka |
| Auth | RS256 JWT / JWKS / Policy-based ACM |
| Observability | OpenTelemetry / Structured Logging |
| Container | Docker (multi-stage build) |

## Key Features

### 🤖 Agent Runtime
- **ReAct Loop** with tool calling, reflexion (self-correction), and sub-agent orchestration
- **Intent classification** for routing to specialized agents
- **Declarative YAML config** — define agent behavior without code changes, hot-reload

### 🔧 MCP Protocol Tools
- **stdio mode** (TypeScript): memory-store, local-file-search
- **HTTP mode** (Java): memory-server, code-exec-server, file-search-server
- **Agent SDK**: `@tool` decorator + `ToolContext` + `ToolTestHarness` for plugin development

### 🧠 Model Router
- Multi-provider support: OpenAI / Anthropic / Local Ollama
- Provider-level fallback + token budget control
- Call provenance via Kafka events

### 📚 RAG Pipeline
- Multi-format chunking: markdown, code (AST-aware), PDF, OpenAPI spec
- Embedding + vector retrieval (in-memory / Redis pluggable)
- Hybrid reranking: LLM listwise + optional cross-encoder + learned reranker with feedback

### 📊 Eval Harness
- 3-axis evaluation: correctness / efficiency / safety
- Controlled experiment framework (4-arm A/B testing)
- CI-integrated eval gate to block quality regression

### 🔐 Platform Engineering
- **Auth Service**: RS256 JWT + JWKS endpoint + kid-based key rotation + multi-tenant policy
- **Memory Server**: PostgreSQL full-text search (tsvector+GIN) with TF-IDF fallback + Redis L1 cache
- **Code Exec Server**: Docker sandbox with OWASP hardening + resource limits

## Quick Start

```bash
# Prerequisites: JDK 21, Python 3.11+, Node 18+, Docker

# Build all
make build

# Run tests
make test          # All (Java + Python)
make test-java     # Java only
make test-python   # Python only

# Start services
docker-compose -f platform/docker-compose.yml up -d
```

## Project Structure

```
├── platform/
│   ├── agent-server/        # Python: FastAPI + LangGraph ReAct agent
│   ├── agent-server-java/   # Java: Spring AI ChatClient (same API, fallback)
│   ├── agent-sdk/           # Python: Plugin toolkit (@tool, ToolContext)
│   ├── auth-service/        # Java: RS256 JWT auth + JWKS + policy ACM
│   ├── memory-server/       # Java: TF-IDF search + Redis cache + multi-tenant
│   ├── codeexec-server/     # Java: Docker-isolated code execution
│   ├── filesearch-server/   # Java: Sandboxed file operations
│   ├── completion-server/   # Java: FIM code completion service
│   ├── model-router/        # Java: LLM model routing + fallback
│   ├── review-agent/        # Java: Spring AI code review agent
│   ├── eval-harness/        # Python: 3-axis eval + controlled experiments
│   ├── mcp-common/          # Java: Shared security lib
│   ├── common-spi/          # Java: Service discovery SPI
│   ├── mcp-cli/             # TypeScript: CLI tools + memory management
│   ├── frontend/            # React 19 + Vite
│   └── docs/                # Design documents + API reference
├── src/                     # TypeScript MCP servers (stdio)
├── tools/                   # TypeScript: memory-ts
├── Makefile
└── package.json
```

## Testing

500+ test cases across Java (JUnit 5 + Testcontainers) and Python (pytest + asyncio).

## License

MIT
