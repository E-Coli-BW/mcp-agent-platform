# MCP Agent Platform

A full-stack **Agent runtime platform** built for learning and demonstrating distributed systems + LLM agent engineering.

This is a personal learning project developed with AI coding agents, not a
production deployment. I use it to practice runtime design, tool integration,
review, and validation. The modules below include optional experiments; they
are not all active in the default chat path.

For a small, reproducible example, see the [local MCP demo](#local-mcp-demo).
For the Agent and browser UI, start with the
[Agent Server setup guide](platform/agent-server/README.md#quick-start).

## Architecture

```text
React frontend
	|
FastAPI protocol adapters (OpenAI HTTP/SSE)
	|
Runtime shell
  RunCoordinator | durable runs/events | policy/approval | recovery
  observers: usage | model provenance | Kafka | retrieval feedback
	|
AgentEngine -> Agent registry/cache -> modular agent_graph
	|
Agent capabilities
  execution_policy | tool_routing | compressor | model | skills | delegation
	|
Tool authorization -> local tools / RAG / Java REST tool services
	|
PostgreSQL | Redis | Kafka | Docker sandbox | Ollama/model providers
```

The Agent core does not own HTTP, SSE, Kafka, authentication, or durable run
operations. Those capabilities compose around `AgentEngine` in the runtime
shell.

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

### 🔧 Tool Integration
- **MCP over stdio** (TypeScript): memory-store, local-file-search
- **Custom REST bridge** (Java): memory-server, codeexec-server, filesearch-server.
  The Python client calls `/api/tools/{tool_name}`; this bridge is not an
  implementation of MCP Streamable HTTP.
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
- Baseline comparison CLI (`eval-gate`) with pass-rate and per-case regression checks

The default CI runs build and test checks for Java, Python, and TypeScript.
The eval harness also provides deterministic tests and a coding-fixture
preflight; those are different from running a live-model quality gate.
Live evaluations require an explicitly configured Agent/model and a reviewed
baseline; see the [eval guide](platform/eval-harness/README.md).

### 🔐 Platform Engineering
- **Auth Service**: RS256 JWT + JWKS endpoint + kid-based key rotation + multi-tenant policy
- **Memory Server**: PostgreSQL full-text search (tsvector+GIN), TF-IDF fallback, and optional Redis cache with no-op degradation
- **Code Exec Server**: Docker execution with network disabled, a read-only root
  filesystem, an unprivileged user, dropped capabilities, and CPU/memory/process
  limits. These controls reduce exposure; they are not a guarantee against
  container escape or a certification for executing arbitrary hostile code.
  See the [security policy](SECURITY.md).

## Quick Start

```bash
# Prerequisites: JDK 21, Python 3.11+, Node 18+, Docker

# Build all
make build

# Run deterministic tests
make test          # Java + Python + TypeScript + frontend checks
make test-java     # Java only
make test-python   # Python only

# Start the core local dependencies (Redis + PostgreSQL)
make docker-up

# Optional: start Kafka/UI, Nacos, Elasticsearch, and Jaeger too
make docker-up-full
```

Kafka, tracing, service discovery, and Elasticsearch are optional for ordinary
local Agent chat. `make docker-up-full` waits for healthy services and uses
multi-architecture images, including native ARM64 images on Apple Silicon.

These commands build/test the project and start dependencies, not the Agent
application itself. Continue with the
[Agent Server setup guide](platform/agent-server/README.md#quick-start) to
configure a model and start the API/UI. Optional services and model-dependent
features need their own configuration.

## Local MCP Demo

This read-only terminal demo starts the real TypeScript file-search server,
connects through the MCP SDK over stdio, discovers its tools, and reads the
first three lines of this README. No model, cloud credentials, Java services,
or Docker are required.

From the repository root, with Node.js 20+:

```bash
npm ci --prefix platform/mcp-cli --workspaces=false
npm run build --prefix platform/mcp-cli
node platform/mcp-cli/demo.mjs
```

Output includes:

```text
MCP stdio: connected to local-file-search
tools/list: read_file_content available
tools/call: README.md, lines 1-3
    1 | # MCP Agent Platform
    2 |
    3 | A full-stack **Agent runtime platform** built for learning and demonstrating distributed systems + LLM agent engineering.
PASS: file content matches the repository README
```

The client checks the returned content against the actual file and closes the
server connection when finished. This demonstrates the MCP tool boundary,
not model reasoning, a complete coding task, or sandbox isolation. It does
not exercise the separate Java REST bridge.

## Project Structure

```
├── platform/
│   ├── agent-server/        # Python: API/runtime shell + modular AgentEngine
│   ├── agent-server-java/   # Java: Spring AI ChatClient (same API)
│   ├── agent-sdk/           # Python: Plugin toolkit (@tool, ToolContext)
│   ├── auth-service/        # Java: RS256 JWT auth + JWKS + policy ACM
│   ├── memory-server/       # Java: TF-IDF search + Redis cache + multi-tenant
│   ├── codeexec-server/     # Java: Docker-isolated code execution
│   ├── filesearch-server/   # Java: Sandboxed file operations
│   ├── model-router/        # Java: LLM model routing + fallback
│   ├── eval-harness/        # Python: 3-axis eval + controlled experiments
│   ├── mcp-common/          # Java: Shared security lib
│   ├── common-spi/          # Java: SPI interfaces
│   ├── mcp-cli/             # TypeScript: MCP stdio servers + local demo
│   └── frontend/            # React 19 + Vite
├── Makefile
└── package.json
```

## Testing

Java tests use JUnit 5/Testcontainers; Python tests use pytest/asyncio.
TypeScript MCP tests and frontend checks have separate Makefile targets.
Test counts and results depend on the checkout and available dependencies;
the presence of a test or workflow is not evidence that a live Agent task
passed. See [CI configuration](.github/workflows/ci.yml) for the configured
checks and [eval documentation](platform/eval-harness/README.md) for
model-dependent validation.

## License

MIT
