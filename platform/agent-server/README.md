# Coding Agent Server

OpenAI-compatible coding agent with LangGraph, tool use, and RAG.

## Prerequisites

| Dependency | Required? | Install |
|---|---|---|
| **Python 3.11+** | ✅ Yes | `brew install python@3.13` (macOS) |
| **Ollama** | ✅ Yes | `brew install ollama` then `ollama serve` |
| **A model in Ollama** | ✅ Yes | `ollama pull qwen2.5:7b` (default model) |
| **Redis** | ⚠️ Optional | `brew install redis && redis-server` — needed for conversation persistence & RAG vector search. Without it the server still starts but sessions are ephemeral. |
| **Java 21** | ⚠️ Optional | Only if using the MCP tool backends (memory-server, filesearch-server, codeexec-server) |

## Quick Start

> **Important:** Run each command separately. Lines starting with `#` are comments — do not paste them into your terminal.

```bash
# 1. Navigate to the agent-server directory
cd platform/agent-server

# 2. Create a virtual environment
python -m venv .venv

# 3. Activate it
source .venv/bin/activate

# 4. Install dependencies
pip install -e ".[dev]"

# 5. Make sure Ollama is running (in a separate terminal)
ollama serve

# 6. Pull the default model (one-time)
ollama pull qwen2.5:7b

# 7. Start the agent server
python -m app.main
```

The server starts on **http://localhost:8500**.

## Web UIs

### Option 1: Built-in IDE (zero setup)

The agent ships with a built-in VS Code–style IDE. After starting the server:

👉 **Open http://localhost:8500/ui** in your browser.

Features: file explorer, Monaco editor with syntax highlighting, chat panel with streaming, tool call visualization.

### Option 2: Open WebUI (richer chat experience)

[Open WebUI](https://github.com/open-webui/open-webui) provides a ChatGPT-like interface with conversation history, model switching, and more.

```bash
# Install and run Open WebUI via Docker (one command)
docker run -d -p 3000:8080 \
  -e OPENAI_API_BASE_URLS="http://host.docker.internal:8500/v1" \
  -e OPENAI_API_KEYS="sk-unused" \
  --name open-webui \
  --restart always \
  ghcr.io/open-webui/open-webui:main
```

Then:
1. Open **http://localhost:3000**
2. Create an account (local only, first user becomes admin)
3. Go to **Settings → Connections → OpenAI API**
4. Add URL: `http://host.docker.internal:8500/v1`, API key: `sk-unused`
5. Select model **"coding-agent"** in the chat dropdown

> **Without Docker:** If you installed Open WebUI via pip (`pip install open-webui`), go to Settings → Connections and add `http://localhost:8500/v1` as the OpenAI endpoint.

## Test with curl

```bash
curl http://localhost:8500/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"coding-agent","messages":[{"role":"user","content":"hello"}],"stream":false}'
```

## Configuration

All settings load from environment variables (prefix `AGENT_`) or a `.env` file
in the project root. Settings are defined in `app/config.py` as a Pydantic model.

### Config Hierarchy (highest priority wins)

```
1. Environment variables       AGENT_DEFAULT_MODEL=qwen2.5:7b
2. .env file                   platform/agent-server/.env
3. Agent YAML config           agents/coding-agent.yaml (model field)
4. config.py defaults          cheap_model: str = "qwen2.5:7b"
```

> ⚠️ **Key gotcha**: The `model` field in `agents/coding-agent.yaml` overrides
> `AGENT_DEFAULT_MODEL` for that specific agent. If the frontend requests
> `model=coding-agent`, the YAML config's model takes priority.

### Model Strategy — What Goes Where

The platform has THREE model roles with different requirements:

| Role | Config Key | Requirements | Good Default |
|------|-----------|--------------|--------------|
| **Main agent** | `default_model` / YAML `model:` | Tool calling, structured JSON | `qwen2.5:7b` (Ollama) |
| **Strong model** | `strong_model` | Complex reasoning | `openai/gpt-4o` or `qwen2.5:7b` |
| **Cheap model** | `cheap_model` | Fast text→text only | `mlx/Qwen2.5-0.5B-Instruct-4bit` |

**Why the split?** The main agent needs tool calling (function schemas, structured
JSON output) — only 7B+ models handle this. But auxiliary tasks (reflexion grading,
reranking, verification) are simple text→text and can use a much faster small model.

### Feature Dependencies — Which Features Use Which Model

```
┌─────────────────────────────┬──────────────┬─────────────────────────┐
│ Feature                     │ Uses Model   │ Enable With             │
├─────────────────────────────┼──────────────┼─────────────────────────┤
│ Main agent loop             │ default_model│ always on               │
│ Tool calling                │ default_model│ always on               │
│ Reflexion (quality gate)    │ cheap_model  │ AGENT_REFLEXION_ENABLED │
│ LLM reranking              │ cheap_model  │ AGENT_RERANK_STRATEGY=llm│
│ Subagent verifier           │ cheap_model  │ AGENT_SUBAGENT_VERIFIER_ENABLED│
│ Direct tool routing         │ (no model)   │ AGENT_DIRECT_TOOL_ROUTING_ENABLED│
└─────────────────────────────┴──────────────┴─────────────────────────┘
```

> **If `cheap_model` features are all disabled, MLX has zero effect.** The default
> `config.py` ships with everything off. Enable features in `.env` to activate.

### Model Providers — URI Format

The model name determines which backend is used:

| Prefix | Provider | Example | Notes |
|--------|----------|---------|-------|
| _(none)_ | Ollama | `qwen2.5:7b` | Default, local, free |
| `openai/` | OpenAI | `openai/gpt-4o` | Needs `AGENT_OPENAI_API_KEY` |
| `anthropic/` | Anthropic | `anthropic/claude-sonnet-4-20250514` | Needs `AGENT_ANTHROPIC_API_KEY` |
| `mlx/` | MLX local server | `mlx/Qwen2.5-0.5B-Instruct-4bit` | Needs MLX server on `:8600` |

### Quick Configs

**Local dev (fast, with MLX):**
```env
AGENT_DEFAULT_MODEL=qwen2.5:7b
AGENT_CHEAP_MODEL=mlx/Qwen2.5-0.5B-Instruct-4bit
AGENT_MLX_BASE_URL=http://localhost:8600
AGENT_REFLEXION_ENABLED=true
AGENT_RERANK_STRATEGY=llm
```

**Local dev (simple, no extras):**
```env
AGENT_DEFAULT_MODEL=qwen2.5:7b
# Everything else uses defaults (reflexion off, heuristic rerank)
```

**Cloud production:**
```env
AGENT_DEFAULT_MODEL=openai/gpt-4o
AGENT_STRONG_MODEL=openai/gpt-4o
AGENT_CHEAP_MODEL=openai/gpt-4o-mini
AGENT_REFLEXION_ENABLED=true
AGENT_RERANK_STRATEGY=llm
```

### All Settings Reference

| Variable | Default | Description |
|---|---|---|
| `AGENT_DEFAULT_MODEL` | `qwen2.5:7b` | Main agent model (must support tool calling) |
| `AGENT_STRONG_MODEL` | `qwen2.5:7b` | Complex reasoning model |
| `AGENT_CHEAP_MODEL` | `mlx/Qwen2.5-0.5B-Instruct-4bit` | Fast auxiliary model (grading, reranking) |
| `AGENT_MLX_BASE_URL` | `http://localhost:8600` | MLX server URL (for `mlx/` models) |
| `AGENT_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API endpoint |
| `AGENT_REFLEXION_ENABLED` | `false` | Enable quality gate (adds 1 cheap_model call) |
| `AGENT_REFLEXION_MIN_GRADE` | `3` | Min grade to pass (1-5 scale) |
| `AGENT_RERANK_STRATEGY` | `auto` | `auto`, `llm`, `cross_encoder`, `heuristic`, `none` |
| `AGENT_SUBAGENT_VERIFIER_ENABLED` | `false` | Grade subagent answers before returning |
| `AGENT_DIRECT_TOOL_ROUTING_ENABLED` | `false` | Skip LLM for obvious single-tool reads |
| `AGENT_REDIS_URL` | `redis://localhost:6379/0` | Redis (optional, for persistence) |
| `AGENT_OPENAI_API_KEY` | _(empty)_ | For `openai/` models |
| `AGENT_ANTHROPIC_API_KEY` | _(empty)_ | For `anthropic/` models |
| `AGENT_FALLBACK_MODEL` | _(empty)_ | Auto-failover model when primary is down |
| `AGENT_PROMPT_VERSION` | `v2` | System prompt version |
| `AGENT_CONFIG_DIR` | `agents` | YAML agent config directory |
| `AGENT_JWT_SECRET` | _(dev default)_ | **Set in production!** |

## Architecture

```
FastAPI (OpenAI-compatible API) → http://localhost:8500
  ↓
LangGraph ReAct Agent (PLAN → ACT → SUMMARIZE)
  ↓ prompt modifier (context compression + workspace context + retry)
  ↓ model routing (simple→cheap, complex→strong)
  ↓
Tools (15) → Java MCP Backends (optional)
  ├── memory-server:8180 (memory_search, memory_set, memory_context)
  ├── codeexec-server:8380 (code_run, code_shell)
  └── Local: file_read, file_write, file_edit, file_list, file_search,
             git_status, git_diff, git_commit, run_tests, rag_search

Features:
  ├── Named SSE events (tool_start, tool_end, status)
  ├── Declarative YAML agent configs (agents/coding-agent.yaml)
  ├── Config hot-reload (watchfiles / polling fallback)
  ├── Session LANE (Redis lock prevents concurrent request interleaving)
  ├── Workspace auto-detection (project type, modules, README)
  ├── Active file context (frontend sends open file path)
  ├── Smart compression (AST-aware for code, head+tail for listings)
  └── Conversation persistence (Redis / PostgreSQL / in-memory fallback)
```

## Running Tests

```bash
source .venv/bin/activate
pytest tests/ -q
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `state_modifier` TypeError | Already fixed — uses `prompt=` parameter for LangGraph ≥1.2 |
| `Connection refused` on port 11434 | Ollama isn't running. Start with `ollama serve` |
| `No model found` | Run `ollama pull qwen2.5:7b` first |
| `pip install` fails with 401 | Your pip index requires auth. Try `pip install -e ".[dev]" -i https://pypi.org/simple/` |
| Redis connection errors | Redis is optional. The server works without it (ephemeral sessions) |

## Port: 8500
