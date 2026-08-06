# Phase 7: Plugin SDK & Multi-Domain RAG — Design Document

> **Goal**: Enable external teams to build and deploy agent tools without modifying the core agent server.
> **Prerequisite**: Phases 1-6 complete (declarative agents, tool registry, hot-reload).
> **Effort**: ~2 weeks (SDK: 1 week, pluggable RAG: 3 days, integration: 2 days)

---

## 1. Problem Statement

The current architecture requires tool authors to:
1. Commit code to the agent server repo (`tools/definitions.py` or `tools/FileTools.java`)
2. Understand the agent server internals (TenantContext, ToolRegistry, truncation)
3. Restart the server to deploy new tools
4. Share secrets via env vars on the agent server

This doesn't scale when multiple teams want to build domain-specific agents:
- Call center team needs `ticket_create`, `escalate`, `api_field_lookup`
- DevOps team needs `kubectl_exec`, `docker_logs`, `alert_acknowledge`
- Data team needs `sql_query`, `chart_render`, `report_generate`

Each team should own their tools independently.

---

## 2. Architecture

### 2.1 Overview

```
BEFORE (monolith tools):
  agent-server/tools/ ← everyone commits here

AFTER (plugin architecture):
  agent-server/
  ├── tools/builtin/          ← core tools (file_*, git_*, memory_*)
  └── plugins/                ← drop JAR/wheel here, auto-loaded
      ├── call-center-tools/
      │   ├── plugin.yaml
      │   ├── ticket_tool.py  (or .jar)
      │   └── escalate_tool.py
      └── devops-tools/
          ├── plugin.yaml
          └── kubectl_tool.py

  agent-sdk/                  ← separate library, published to PyPI/Maven
  ├── agent-sdk-python/       (pip install agent-sdk)
  └── agent-sdk-java/         (maven: com.example:agent-sdk)
```

### 2.2 Plugin Lifecycle

```
1. DISCOVER — server scans plugins/ directory on startup
2. VALIDATE — check plugin.yaml schema, verify permissions
3. LOAD     — import tool classes (Python: importlib, Java: classloader)
4. REGISTER — add to ToolRegistry with namespace prefix
5. INJECT   — provide ToolContext (tenant, memory, kb, secrets)
6. WRAP     — apply truncation, error handling, audit logging, rate limiting
7. SERVE    — tools available for agent YAML configs to reference
8. RELOAD   — file watcher detects plugin changes, re-register
```

### 2.3 Component Diagram

```
┌─────────────────────────────────────────────────────────┐
│                     Agent Server                         │
│                                                          │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ Agent    │  │ ToolRegistry │  │ PluginLoader      │ │
│  │ YAML     │→ │              │← │ - scan plugins/   │ │
│  │ configs  │  │ builtin:     │  │ - validate YAML   │ │
│  │          │  │   file_read  │  │ - load classes     │ │
│  │ tools:   │  │   git_commit │  │ - inject context   │ │
│  │ - file_* │  │   memory_*   │  │ - wrap with guards │ │
│  │ - ticket │  │ plugins:     │  │ - watch for changes│ │
│  │          │  │   ticket_*   │  └───────────────────┘ │
│  └──────────┘  │   kubectl_*  │                         │
│                └──────────────┘                         │
│                       │                                  │
│              ┌────────▼────────┐                        │
│              │  ToolContext    │                        │
│              │  - tenant_id   │                        │
│              │  - session_id  │                        │
│              │  - memory      │                        │
│              │  - kb (RAG)    │                        │
│              │  - secrets     │                        │
│              └─────────────────┘                        │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Agent SDK

### 3.1 Python SDK (`agent-sdk-python`)

```
agent-sdk-python/
├── pyproject.toml
├── agent_sdk/
│   ├── __init__.py           # exports: tool, ToolContext, ToolTestHarness
│   ├── tool.py               # @tool decorator with metadata
│   ├── context.py            # ToolContext: tenant, memory, kb, secrets
│   ├── memory.py             # MemoryClient: search/set/get
│   ├── knowledge.py          # KnowledgeBaseClient: search indexed docs
│   ├── testing.py            # ToolTestHarness: mock context for unit tests
│   └── plugin.py             # PluginManifest: parse plugin.yaml
└── tests/
```

**Core API:**

```python
from agent_sdk import tool, ToolContext

@tool(
    name="ticket_create",
    description="Create a support ticket in Jira",
    permissions=["tickets:write"],
)
def ticket_create(ctx: ToolContext, summary: str, priority: str = "medium") -> str:
    """Create a Jira ticket."""
    # ctx.tenant_id — from JWT, automatic
    # ctx.secrets["JIRA_TOKEN"] — injected from vault/env
    # ctx.memory.set(key, value) — cross-session memory
    # ctx.knowledge.search(query, top_k) — RAG search
    response = requests.post(
        f"{ctx.secrets['JIRA_URL']}/rest/api/3/issue",
        json={"fields": {"summary": summary, "priority": {"name": priority}}},
        headers={"Authorization": f"Bearer {ctx.secrets['JIRA_TOKEN']}"},
    )
    return f"✅ Created ticket {response.json()['key']}"
```

**ToolContext interface:**

```python
@dataclass
class ToolContext:
    tenant_id: str                    # from JWT — never trust client input
    session_id: str                   # current conversation session
    user_role: str                    # "CUSTOMER" | "CCC_AGENT" | "ADMIN"
    memory: MemoryClient              # cross-session memory (memory_set/search)
    knowledge: KnowledgeBaseClient    # RAG search across indexed docs
    secrets: dict[str, str]           # injected secrets (JIRA_TOKEN, etc.)
    workspace: str                    # current workspace path
```

**Test harness:**

```python
from agent_sdk.testing import ToolTestHarness

def test_ticket_create():
    harness = ToolTestHarness(
        tenant_id="test-tenant",
        secrets={"JIRA_TOKEN": "fake-token", "JIRA_URL": "https://mock"},
    )
    # Mock HTTP calls
    with harness.mock_http(responses={"POST /rest/api/3/issue": {"key": "TEST-123"}}):
        result = ticket_create(harness.context, summary="Bug report", priority="high")
    assert "TEST-123" in result
```

### 3.2 Java SDK (`agent-sdk-java`)

```
agent-sdk-java/
├── pom.xml
└── src/main/java/com/example/sdk/
    ├── Tool.java                  # interface: String execute(ToolContext, Map params)
    ├── AgentTool.java             # @AgentTool annotation (name, description, permissions)
    ├── ToolContext.java           # tenant, memory, kb, secrets
    ├── MemoryClient.java          # memory operations
    ├── KnowledgeBaseClient.java   # RAG search
    ├── ToolTestHarness.java       # unit test support
    └── PluginManifest.java        # parse plugin.yaml
```

### 3.3 Plugin Manifest (`plugin.yaml`)

```yaml
plugin:
  id: call-center-tools
  name: "Call Center Tool Pack"
  version: "1.0.0"
  author: "Customer Support Team"
  sdk-version: ">=1.0.0"           # minimum SDK version
  language: python                   # or: java

  tools:
    - name: ticket_create
      module: call_center.ticket_tool    # Python module path
      # class: com.teamA.TicketTool      # Java class (alternative)
      description: "Create a support ticket in Jira"
      permissions:
        - tickets:write
      config:                            # tool-specific config
        jira_project_key: ${JIRA_PROJECT}

    - name: escalate
      module: call_center.escalate_tool
      description: "Escalate issue to human agent"
      permissions:
        - escalation:write

    - name: api_field_lookup
      module: call_center.api_lookup
      description: "Look up API spec fields"
      permissions:
        - api:read

  secrets:                               # required secrets (env vars or vault)
    - JIRA_TOKEN
    - JIRA_URL
    - SLACK_WEBHOOK_URL

  knowledge:                             # RAG collections this plugin uses
    - collection: api-specs
      chunker: openapi                   # use OpenAPI-aware chunker
    - collection: product-faqs
      chunker: markdown                  # use Markdown heading chunker

  health_check:                          # optional readiness probe
    endpoint: ${JIRA_URL}/rest/api/3/serverInfo
    interval: 60s
```

---

## 4. Pluggable RAG Chunkers

### 4.1 Problem

Current chunker only handles code (tree-sitter AST). Domain-specific agents need:
- Markdown docs → split by headings
- OpenAPI specs → split by endpoints + schemas
- PDF manuals → split by pages/sections
- FAQ documents → split by Q/A pairs
- CSV/Excel → split by rows with header context

### 4.2 Chunker Registry

```python
# rag/chunker_registry.py
CHUNKER_MAP: dict[str, Callable] = {
    # Code (existing)
    ".py": tree_sitter_chunk,
    ".java": tree_sitter_chunk,
    ".js": tree_sitter_chunk,
    ".ts": tree_sitter_chunk,

    # Documents (new)
    ".md": markdown_chunk,
    ".yaml": openapi_chunk,       # detects if it's an OpenAPI spec
    ".json": openapi_chunk,       # detects if it's an OpenAPI spec
    ".pdf": pdf_chunk,
    ".html": html_chunk,
    ".txt": fixed_size_chunk,
    ".csv": csv_chunk,
}

# Plugin manifests can also register custom chunkers:
# plugin.yaml → knowledge.chunker: "openapi" → uses openapi_chunk
```

### 4.3 New Chunker Implementations

**MarkdownChunker** — split by headings:
```python
def markdown_chunk(file_path: Path) -> list[Chunk]:
    """Split Markdown by ## headings into semantic sections."""
    content = file_path.read_text()
    sections = re.split(r'^(#{1,3}\s+.+)$', content, flags=re.MULTILINE)
    chunks = []
    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        if body.strip():
            chunks.append(Chunk(
                content=heading + "\n" + body.strip(),
                name=heading.lstrip("#").strip(),
                chunk_type="section",
                ...
            ))
    return chunks
```

**OpenApiChunker** — split by endpoints + schemas:
```python
def openapi_chunk(file_path: Path) -> list[Chunk]:
    """Split OpenAPI spec into per-endpoint and per-schema chunks."""
    spec = yaml.safe_load(file_path.read_text())
    if "openapi" not in spec and "swagger" not in spec:
        return fixed_size_chunk(file_path)  # not an API spec, fallback

    chunks = []
    # Each endpoint becomes a chunk
    for path, methods in spec.get("paths", {}).items():
        for method, details in methods.items():
            content = format_endpoint(method, path, details)
            chunks.append(Chunk(content=content, name=f"{method.upper()} {path}", ...))

    # Each schema becomes a chunk (for field lookup queries)
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        content = format_schema(name, schema)
        chunks.append(Chunk(content=content, name=f"Schema: {name}", ...))

    return chunks
```

**PdfChunker** — split by pages (requires `PyPDF2`):
```python
def pdf_chunk(file_path: Path) -> list[Chunk]:
    """Split PDF by pages, with overlap for context."""
    from PyPDF2 import PdfReader
    reader = PdfReader(str(file_path))
    chunks = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            chunks.append(Chunk(
                content=text.strip(),
                name=f"Page {i + 1}",
                start_line=i + 1,
                end_line=i + 1,
                chunk_type="page",
                ...
            ))
    return chunks
```

### 4.4 Per-Tenant Knowledge Isolation

```
Current:  ~/.mcp-local/rag-index/{workspace}-{hash}/
New:      ~/.mcp-local/rag-index/{tenant_id}/{collection}/

Example:
  /rag-index/company-a/api-specs/       ← Company A's API docs
  /rag-index/company-a/product-faqs/    ← Company A's FAQ
  /rag-index/company-b/api-specs/       ← Company B's API docs (different product)

rag_search tool reads TenantContext.get() → searches only that tenant's collections.
```

---

## 5. Implementation Plan

### Phase 7a: Pluggable RAG Chunkers (3 days)

Both Python and Java agent servers need chunker implementations.

| Task | Python File | Java File | Effort |
|---|---|---|---|
| Rename `CodeChunk` → `Chunk` | `rag/chunker.py` | `rag/CodeChunk.java` → `Chunk.java` | 30min |
| `ChunkerRegistry` | `rag/chunker_registry.py` | `rag/ChunkerRegistry.java` | 1h×2 |
| `MarkdownChunker` | `rag/markdown_chunker.py` | `rag/MarkdownChunker.java` | 2h×2 |
| `OpenApiChunker` | `rag/openapi_chunker.py` | `rag/OpenApiChunker.java` | 3h×2 |
| `PdfChunker` | `rag/pdf_chunker.py` | `rag/PdfChunker.java` (Apache PDFBox) | 2h×2 |
| `FixedSizeChunker` | `rag/fixed_size_chunker.py` | `rag/FixedSizeChunker.java` | 1h×2 |
| Per-tenant index directory | `rag/indexer.py` | `rag/RagIndexer.java` | 2h |
| Update `rag_search` for tenant isolation | `tools/rag_tool.py` | `tools/RagSearchTool.java` | 1h×2 |
| Tests | `tests/test_chunkers.py` | `*ChunkerTest.java` | 2h×2 |

**Note**: The Python↔Java translation can be partially automated — see §8 below.

### Phase 7b: Agent SDK (1 week)

| Task | Effort |
|---|---|
| `agent-sdk-python` package scaffold | 1h |
| `@tool` decorator with metadata (name, desc, permissions) | 2h |
| `ToolContext` (tenant, session, secrets) | 2h |
| `MemoryClient` (wrapper around memory-server REST API) | 2h |
| `KnowledgeBaseClient` (wrapper around rag_search) | 2h |
| `ToolTestHarness` (mock context, mock HTTP) | 3h |
| Plugin manifest parser (`plugin.yaml` → `PluginManifest`) | 2h |
| `PluginLoader` (scan, validate, import, register) | 4h |
| Permission enforcement (check JWT roles vs tool permissions) | 2h |
| Secret injection (from env vars, with validation) | 1h |
| Hot-reload (file watcher on plugins/ directory) | 1h |
| Example plugin: call-center-tools | 2h |
| SDK tests | 3h |
| Java SDK equivalent (`agent-sdk-java`) | 3 days |

### Phase 7c: Integration (2 days)

| Task | Effort |
|---|---|
| Wire PluginLoader into agent server startup | 2h |
| Plugin tools available in agent YAML `tools:` list | 1h |
| Update `ToolRegistry` to handle namespaced tool names | 1h |
| Update docs: plugin development guide | 3h |
| End-to-end test: call-center agent with plugin tools | 2h |
| Performance test: 50 plugins × 10 tools each | 2h |

---

## 6. Example: Call Center Agent (End-to-End)

### Step 1: Build the plugin (separate repo)

```
call-center-plugin/
├── pyproject.toml           # depends on agent-sdk
├── plugin.yaml              # manifest
├── call_center/
│   ├── ticket_tool.py       # @tool ticket_create
│   ├── escalate_tool.py     # @tool escalate
│   └── api_lookup.py        # @tool api_field_lookup
└── tests/
    └── test_tools.py        # uses ToolTestHarness
```

### Step 2: Deploy the plugin

```bash
# Copy plugin to agent server
cp -r call-center-plugin/ platform/agent-server/plugins/call-center-tools/

# Index the knowledge base (per-tenant)
TENANT_ID=company-a python -m app.rag.index.indexer /docs/company-a/api-specs/ --collection api-specs
TENANT_ID=company-a python -m app.rag.index.indexer /docs/company-a/faqs/ --collection product-faqs
```

### Step 3: Create the agent YAML

```yaml
# agents/call-center-agent.yaml
id: call-center-agent
name: "Call Center Help Desk"
model: openai/gpt-4o
prompt: |
  You are a Call Center Help Desk agent...
tools:
  - rag_search              # builtin
  - memory_search            # builtin
  - memory_set               # builtin
  - ticket_create            # from call-center-tools plugin
  - escalate                 # from call-center-tools plugin
  - api_field_lookup         # from call-center-tools plugin
```

### Step 4: Use it

```bash
curl http://localhost:8500/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"model":"call-center-agent","messages":[{"role":"user","content":"Does Product X API have a customer_tier field?"}]}'
```

The agent:
1. Calls `rag_search("Product X API customer_tier field")`
2. RAG finds the OpenAPI spec chunk: `Schema: CustomerProfile → customer_tier: string`
3. Returns: "Yes, Product X API has a `customer_tier` field (string type, values: bronze/silver/gold). See API spec CustomerProfile schema."

---

## 7. What Reuses from Existing Platform

| Existing Component | Reused For | Changes Needed |
|---|---|---|
| ToolRegistry | Register plugin tools | Add namespace support |
| TenantContext | Per-tenant tool execution | None |
| JWT auth + RBAC | Permission enforcement | Check tool permissions vs JWT roles |
| Kafka events | Audit logging for plugin tools | None |
| ConversationStore | Session context for plugins | None |
| ConfigWatcher | Hot-reload plugins | Extend to watch plugins/ dir |
| AgentFactory | Create agents with plugin tools | Resolve plugin tool names |
| SSE streaming | tool_start/tool_end events | None |
| Rate limiting | Per-tenant tool rate limits | None |
| Memory service | Plugin cross-session memory | Expose via SDK MemoryClient |
| RAG retriever | Plugin knowledge search | Expose via SDK KnowledgeBaseClient |
| Embedder + Redis store | Index plugin knowledge bases | None |

**Reuse rate: ~85%** — the SDK is mostly a packaging + interface exercise over existing infrastructure.

---

## 8. Automating Python↔Java Translation

### 8.1 The Problem

Maintaining two implementations (Python `agent-server` + Java `agent-server-java`) of the same logic creates sync drift. When someone adds `MarkdownChunker.py`, they must also write `MarkdownChunker.java`. This is error-prone and doubles the effort.

### 8.2 Approaches

| Approach | Pros | Cons | Best For |
|---|---|---|---|
| **A. LLM-assisted translation** | Fast, handles idioms | Needs review, may miss edge cases | One-off ports (current approach) |
| **B. Shared test suite** | Guarantees behavior parity | Doesn't write code | Regression detection |
| **C. Code generation from spec** | Single source of truth | Limited to simple logic | DTOs, tool stubs, configs |
| **D. Shared interface + language-specific impl** | Clean contracts | Still need both impls | SDK interfaces |

### 8.3 Recommended: Shared Contract Tests + LLM Fleet Translation

**Strategy**: Write the Python implementation first (faster iteration), then use a fleet prompt to translate to Java, validated by shared contract tests.

**Step 1: Define contract tests in a language-neutral format**

```yaml
# tests/contracts/markdown_chunker.yaml
suite: MarkdownChunker
tests:
  - name: should_split_by_h2_headings
    input:
      file_content: |
        # Title
        Intro paragraph.
        ## Section A
        Content A.
        ## Section B
        Content B.
    expected:
      chunk_count: 3
      chunks:
        - name: "Title"
          chunk_type: section
        - name: "Section A"
          chunk_type: section
          content_contains: "Content A"
        - name: "Section B"
          chunk_type: section

  - name: should_handle_empty_file
    input:
      file_content: ""
    expected:
      chunk_count: 0
```

**Step 2: Generate test implementations from contracts**

```python
# Auto-generated from contracts/markdown_chunker.yaml
def test_should_split_by_h2_headings():
    chunks = markdown_chunk(create_temp_file(CONTENT))
    assert len(chunks) == 3
    assert chunks[1].name == "Section A"
    assert "Content A" in chunks[1].content
```

```java
// Auto-generated from contracts/markdown_chunker.yaml
@Test
void should_split_by_h2_headings() {
    var chunks = chunker.chunkFile(createTempFile(CONTENT));
    assertEquals(3, chunks.size());
    assertEquals("Section A", chunks.get(1).name());
    assertTrue(chunks.get(1).content().contains("Content A"));
}
```

**Step 3: Translate implementation with fleet prompt**

```
Given this Python implementation of MarkdownChunker:
<python code>

And this Java test that it must pass:
<generated Java test>

Write the Java implementation. Follow these conventions:
- @Component, constructor injection, final fields
- Java 21 records for return types
- No Lombok
- Match the exact output format of the Python version
```

### 8.4 What Can Be Fully Auto-Generated

| Component | Auto-generate? | How |
|---|---|---|
| **DTOs/Records** | ✅ Yes | Python dataclass → Java record (field names + types) |
| **Tool stubs** | ✅ Yes | Python `@tool` → Java `FunctionCallback` scaffold |
| **Config properties** | ✅ Yes | Python `Settings` → Java `@ConfigurationProperties` |
| **Contract tests** | ✅ Yes | YAML spec → both Python + Java test code |
| **Chunkers** | 🔶 Partial | Logic is translatable, but Python regex ≠ Java regex |
| **Agent loop** | ❌ No | LangGraph ≠ Spring AI — fundamentally different APIs |
| **Security filters** | ❌ No | Servlet ≠ WebFlux — different paradigms |

### 8.5 CI Guard Against Drift

Add a CI check that ensures both implementations pass the same contract tests:

```yaml
# .github/workflows/contract-tests.yml
jobs:
  python-contracts:
    steps:
      - run: cd platform/agent-server && pytest tests/contracts/ -q
  java-contracts:
    steps:
      - run: cd platform/agent-server-java && mvn test -Dtest="*ContractTest"

  verify-parity:
    needs: [python-contracts, java-contracts]
    steps:
      - run: echo "Both implementations pass the same contracts ✅"
```

### 8.6 Current Sync Status

| Component | Python | Java | In Sync? |
|---|---|---|---|
| TreeSitterChunker | AST-based (tree-sitter) | Regex-based | ⚠️ Different quality |
| OllamaEmbedder | httpx async | WebClient reactive | ✅ Same API |
| HybridRetriever | Vector + BM25 + RRF | Vector + BM25 + RRF | ✅ Same algorithm |
| IntentClassifier | Regex patterns | Same regex patterns | ✅ Same behavior |
| PromptService | V1/V2 prompts | V1/V2 prompts | ✅ Same text |
| SSE events | tool_start/end/status | tool_start/end/status | ✅ Same format |
| Session LANE | Redis SET NX | ReactiveRedis SET NX | ✅ Same pattern |
| ContextCompressor | AST-aware + head/tail | Head/tail only | ⚠️ Java lacks AST |
| AgentService | LangGraph streaming | Spring AI blocking | ⚠️ Different streaming |
| MarkdownChunker | Not yet | Not yet | — Phase 7 |
| OpenApiChunker | Not yet | Not yet | — Phase 7 |
