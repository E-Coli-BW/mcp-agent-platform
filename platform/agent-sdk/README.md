# Agent SDK — Plugin Development Guide

> Build and deploy custom agent tools without modifying the core agent server.

---

## Quick Start (5 minutes)

### 1. Create a plugin directory

```bash
mkdir -p my-plugin/my_tools
```

### 2. Write a tool

```python
# my-plugin/my_tools/hello.py
from agent_sdk import tool, ToolContext

@tool(name="hello", description="Greet a user")
def hello(ctx: ToolContext, name: str = "World") -> str:
    return f"👋 Hello, {name}! (tenant: {ctx.tenant_id})"
```

### 3. Create `plugin.yaml`

```yaml
# my-plugin/plugin.yaml
plugin:
  id: my-plugin
  name: "My Plugin"
  version: "1.0.0"
  language: python
  tools:
    - name: hello
      module: my_tools.hello
      description: "Greet a user"
```

### 4. Deploy (copy to plugins directory)

```bash
cp -r my-plugin/ platform/agent-server/plugins/my-plugin/
# File watcher detects the new plugin — no restart needed
```

### 5. Add to an agent config

```yaml
# agents/my-agent.yaml
id: my-agent
name: "My Agent"
model: qwen2.5:7b
prompt: "You are a helpful assistant."
tools:
  - hello           # your plugin tool
  - rag_search      # builtin
  - memory_search   # builtin
```

### 6. Call it

```bash
curl http://localhost:8500/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model":"my-agent","messages":[{"role":"user","content":"Say hi to Alice"}]}'
```

---

## SDK API Reference

### `@tool` Decorator

```python
from agent_sdk import tool

@tool(
    name="tool_name",                    # unique identifier
    description="What this tool does",   # shown to the LLM
    permissions=["scope:action"],        # required JWT permissions (optional)
)
def my_tool(ctx: ToolContext, param1: str, param2: int = 10) -> str:
    """Detailed description (used as LLM tool docstring)."""
    return "result string"
```

**Rules:**
- First parameter must be `ctx: ToolContext`
- Must return `str`
- Catch all errors internally — return `"❌ Error: ..."` instead of raising

### `ToolContext`

Injected automatically into every tool call:

```python
ctx.tenant_id       # "company-a" — from JWT, identifies the organization
ctx.session_id      # current conversation session
ctx.user_role       # "USER" | "ADMIN" | "CCC_AGENT"
ctx.secrets          # {"JIRA_TOKEN": "...", "API_KEY": "..."} — from env vars
ctx.workspace       # current workspace path
ctx.memory          # MemoryClient — persistent cross-session memory
ctx.knowledge       # KnowledgeBaseClient — RAG search over indexed docs
```

### `MemoryClient`

```python
await ctx.memory.search("query", namespace="ns", top_k=10)  # search memories
await ctx.memory.set("key", "value", tags=["tag1"])          # save memory
await ctx.memory.get("key")                                   # get by key
```

### `KnowledgeBaseClient`

```python
results = await ctx.knowledge.search("query", top_k=5)
# Returns: [SearchResult(content, file_path, name, score)]
```

### `ToolTestHarness`

Unit test tools without running the server:

```python
from agent_sdk import ToolTestHarness

def test_my_tool():
    harness = ToolTestHarness(
        tenant_id="test",
        secrets={"API_KEY": "fake"},
    )
    result = my_tool(harness.context, param1="test")
    assert "expected" in result
```

---

## Plugin Manifest (`plugin.yaml`)

```yaml
plugin:
  id: my-plugin                    # unique identifier
  name: "Human-Readable Name"
  version: "1.0.0"
  author: "Team Name"             # optional
  language: python                 # python | java

  tools:
    - name: tool_name
      module: package.module       # Python import path (relative to plugin dir)
      description: "What it does"
      permissions: [scope:action]  # optional — checked against JWT roles

  secrets:                         # required env vars (validated at load time)
    - API_KEY
    - API_URL

  knowledge:                       # RAG collections (optional)
    - collection: my-docs
      chunker: markdown            # markdown | openapi | pdf | html | fixed_size
```

---

## Supported RAG Chunkers

| Chunker | Extensions | Splits By | Best For |
|---|---|---|---|
| `tree_sitter` | .py .java .js .ts | Functions, classes (AST) | Source code |
| `markdown` | .md | `#` `##` `###` headings | Docs, FAQs, wikis |
| `openapi` | .yaml .json | Endpoints + schemas | API specs |
| `pdf` | .pdf | Pages | Manuals, reports |
| `html` | .html | `<h1>`-`<h3>` tags | Web pages |
| `fixed_size` | .txt .csv | Every 50 lines (5-line overlap) | Plain text |

### Index Knowledge

```bash
# Per-tenant (isolated)
python -m app.rag.index.indexer /docs/ --tenant company-a --collection product-docs

# Shared (all tenants)
python -m app.rag.index.indexer /docs/
```

---

## Architecture

```
Your Plugin (separate repo)              Agent Server
────────────────────────                 ────────────────────────
my-plugin/                               plugins/my-plugin/ ← copy here
├── plugin.yaml         ──deploy──→      │
├── my_tools/                            ├── plugin.yaml
│   └── hello.py                         └── my_tools/hello.py
└── tests/                                       │
    └── test_hello.py                    PluginLoader reads plugin.yaml
                                                  │
                                         ToolRegistry registers tools
                                                  │
                                         Agent YAML references tools
                                                  │
                                         LLM calls tool → ToolContext injected
                                                  │
                                         SSE events: tool_start → tool_end
```

**What the SDK handles for you:**
- Tenant isolation (ctx.tenant_id from JWT)
- Secret injection (from env vars)
- Output truncation (3000 chars max)
- Error handling (exceptions → error strings)
- Audit logging (Kafka events)
- Rate limiting (per-tenant)
- Hot-reload (edit plugin → auto-reloaded)
