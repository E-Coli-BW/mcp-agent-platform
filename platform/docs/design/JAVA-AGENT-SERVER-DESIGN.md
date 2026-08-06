# Java Agent Server — Design Document

> **Goal**: Drop-in replacement for the Python agent server (`platform/agent-server/`).
> Same API contract (`POST /v1/chat/completions`), same behavior, pure Java/Spring Boot.
>
> **Module**: `platform/agent-server-java/` (new Spring Boot module)
> **Port**: 8500 (same as Python, swap by stopping one and starting the other)

---

## 1. Why Java?

The Python version depends on LangChain/LangGraph for the agent loop. In Java, we use **Spring AI** which provides:
- `ChatClient` with tool calling (equivalent to LangGraph's `create_react_agent`)
- `FunctionCallback` for tool definitions (equivalent to `@tool` decorator)
- Multi-model support: Ollama, OpenAI, Anthropic via starter dependencies
- SSE streaming via `Flux<ServerSentEvent>`

The result is a **functionally identical** agent server that fits the existing Java ecosystem.

---

## 2. Technology Mapping

| Python | Java Equivalent |
|---|---|
| FastAPI | Spring Boot 3.4 WebFlux |
| LangGraph `create_react_agent` | Spring AI `ChatClient` with `.tools()` |
| `@tool` decorator | `@Bean FunctionCallback` |
| `astream_events()` | `Flux<ChatResponse>` streaming |
| Pydantic `BaseModel` | Java `record` |
| `httpx.AsyncClient` | `WebClient` (non-blocking) |
| Redis `redis.asyncio` | `spring-data-redis` + `ReactiveRedisTemplate` |
| `sse_starlette` | `Flux<ServerSentEvent>` |
| YAML config loading | `@ConfigurationProperties` + `SnakeYAML` |
| `watchfiles` | Java `WatchService` (built-in NIO) |
| `contextvars` (tenant) | `TenantContext` from `mcp-common` |

---

## 3. Module Structure

```
platform/agent-server-java/
├── pom.xml
├── src/main/java/com/example/agent/
│   ├── AgentServerApplication.java        # @SpringBootApplication
│   │
│   ├── api/
│   │   ├── ChatController.java            # POST /v1/chat/completions (SSE + JSON)
│   │   ├── WorkspaceController.java       # /api/workspace/** (file tree, read, open)
│   │   ├── ChatRequest.java               # record: model, messages, stream, session_id, active_file
│   │   └── ChatResponse.java              # OpenAI-compatible response format
│   │
│   ├── agent/
│   │   ├── AgentService.java              # Core agent loop — Spring AI ChatClient
│   │   ├── AgentFactory.java              # Creates ChatClient per model/config
│   │   ├── PromptService.java             # V1/V2 system prompts, workspace context injection
│   │   ├── IntentClassifier.java          # Meta-question detection, topic switch, complexity
│   │   └── ContextCompressor.java         # Smart compression (AST, head+tail, listings)
│   │
│   ├── tools/
│   │   ├── FileTools.java                 # file_read, file_write, file_edit, file_list, file_search
│   │   ├── GitTools.java                  # git_status, git_diff, git_commit
│   │   ├── MemoryTools.java               # memory_search, memory_set, memory_context (→ REST)
│   │   ├── CodeExecTools.java             # code_run, code_shell (→ REST)
│   │   ├── RagSearchTool.java             # rag_search
│   │   ├── ToolRegistry.java              # Name → FunctionCallback resolution
│   │   └── McpRestClient.java             # WebClient wrapper for Java backend calls
│   │
│   ├── config/
│   │   ├── AgentProperties.java           # @ConfigurationProperties(prefix = "agent")
│   │   ├── AgentConfig.java               # YAML agent config record
│   │   ├── AgentConfigLoader.java         # Load agents/*.yaml
│   │   └── ConfigWatcher.java             # NIO WatchService for hot-reload
│   │
│   ├── session/
│   │   ├── ConversationStore.java         # Redis sliding window (20 msgs)
│   │   ├── SessionLane.java               # Redis distributed lock per session
│   │   └── Message.java                   # record: role, content, timestamp
│   │
│   ├── context/
│   │   └── WorkspaceDetector.java         # Project type, modules, README summary
│   │
│   ├── streaming/
│   │   └── SseEventBuilder.java           # Named SSE events (tool_start, tool_end, status)
│   │
│   └── security/
│       └── SecurityConfig.java            # Extends McpSecurityConfigBase from mcp-common
│
├── src/main/resources/
│   ├── application.yml
│   └── agents/
│       └── coding-agent.yaml              # Default agent config (same as Python)
│
└── src/test/java/com/example/agent/
    ├── ChatControllerTest.java
    ├── IntentClassifierTest.java
    ├── FileToolsTest.java
    ├── ContextCompressorTest.java
    └── AgentConfigLoaderTest.java
```

---

## 4. Core Components

### 4.1 Agent Loop — Spring AI ChatClient

This is the Java equivalent of LangGraph's `create_react_agent`:

```java
@Service
public class AgentService {

    private final AgentFactory agentFactory;
    private final PromptService promptService;
    private final ContextCompressor compressor;

    public Flux<ServerSentEvent<String>> streamResponse(
            ChatRequest request, String sessionId, String tenantId) {

        var chatClient = agentFactory.getClient(request.model());
        var systemPrompt = promptService.buildPrompt(tenantId);

        return chatClient.prompt()
            .system(systemPrompt)
            .user(request.lastUserMessage())
            .tools(agentFactory.getTools(request.model()))
            .advisors(new ConversationAdvisor(sessionId))  // message history
            .stream()
            .chatResponse()
            .map(this::toSseEvent);
    }
}
```

### 4.2 Tool Definitions — FunctionCallback

```java
@Configuration
public class FileTools {

    @Bean
    @Description("Read file contents with line numbers. Use start_line/end_line for ranges.")
    public FunctionCallback fileRead() {
        return FunctionCallback.builder()
            .function("file_read", (ReadRequest req) -> {
                // Same logic as Python file_read
                Path resolved = workspace.resolve(req.path());
                List<String> lines = Files.readAllLines(resolved);
                // ... line numbering, truncation ...
                return result;
            })
            .inputType(ReadRequest.class)
            .build();
    }

    record ReadRequest(String path, Integer startLine, Integer endLine) {}
}
```

### 4.3 SSE Streaming — Named Events

```java
public class SseEventBuilder {

    public static ServerSentEvent<String> toolStart(String tool, Map<String, Object> input, int seq) {
        return ServerSentEvent.<String>builder()
            .event("tool_start")
            .data(toJson(Map.of("tool", tool, "input", input, "seq", seq)))
            .build();
    }

    public static ServerSentEvent<String> status(String state, Map<String, Object> extra) {
        return ServerSentEvent.<String>builder()
            .event("status")
            .data(toJson(Map.of("state", state, extra)))
            .build();
    }

    public static ServerSentEvent<String> contentChunk(String content, String model) {
        // OpenAI-compatible format
        return ServerSentEvent.<String>builder()
            .data(toJson(new ChatCompletionChunk(content, model)))
            .build();
    }
}
```

### 4.4 Configuration

```yaml
# application.yml
server:
  port: 8500

agent:
  default-model: qwen2.5:7b
  strong-model: qwen2.5:7b
  cheap-model: qwen2.5:7b
  prompt-version: v2
  config-dir: agents
  max-tool-output: 3000
  max-context-chars: 20000
  workspace: ~/agent-workspace

spring:
  ai:
    ollama:
      base-url: http://localhost:11434
      chat:
        model: qwen2.5:7b
    openai:
      api-key: ${AGENT_OPENAI_API_KEY:}
  data:
    redis:
      url: redis://localhost:6379/0
```

---

## 5. Key Design Decisions

### 5.1 Spring AI vs Raw HTTP to Ollama

Spring AI provides `ChatClient` with built-in tool calling support. This maps directly to what LangGraph does:

```
LangGraph: create_react_agent(model, tools, prompt, checkpointer)
Spring AI: ChatClient.builder(model).defaultTools(tools).defaultSystem(prompt).build()
```

Spring AI handles the ReAct loop internally — it calls the LLM, parses tool call responses, executes tools, feeds results back, and repeats until the LLM returns text.

### 5.2 Conversation History

Spring AI has `MessageChatMemoryAdvisor` + `ChatMemory` interface. We implement `ChatMemory` backed by Redis (same as Python's `ConversationStore`).

### 5.3 Context Compression

Spring AI doesn't have built-in context compression. We implement `ContextCompressor` as a custom `Advisor` that runs before each LLM call — equivalent to Python's `state_modifier`.

### 5.4 Multi-Model Support

Spring AI supports Ollama, OpenAI, and Anthropic via separate starter dependencies. We use the same factory pattern as the Python version:

```java
@Service
public class AgentFactory {
    private final Map<String, ChatClient> clients = new ConcurrentHashMap<>();

    public ChatClient getClient(String modelName) {
        return clients.computeIfAbsent(modelName, this::createClient);
    }

    private ChatClient createClient(String name) {
        ChatModel model = switch (detectProvider(name)) {
            case OLLAMA -> new OllamaChatModel(ollamaApi, OllamaOptions.builder().model(name).build());
            case OPENAI -> new OpenAiChatModel(openAiApi, OpenAiChatOptions.builder().model(name).build());
            case ANTHROPIC -> new AnthropicChatModel(anthropicApi, AnthropicChatOptions.builder().model(name).build());
        };
        return ChatClient.builder(model).defaultTools(getTools(name)).build();
    }
}
```

---

## 6. RAG Pipeline (Ported)

The Python RAG pipeline is fully portable to Java. tree-sitter has a Java SDK (`io.github.tree-sitter:tree-sitter`), and embeddings are just HTTP calls to Ollama.

### 6.1 Module Structure

```
com.example.agent.rag/
├── CodeChunk.java           # record: content, filePath, language, name, startLine, endLine, docstring
├── TreeSitterChunker.java   # tree-sitter AST parsing → CodeChunks
├── OllamaEmbedder.java      # WebClient → Ollama /api/embeddings (batch + retry)
├── HybridRetriever.java     # In-memory vector + BM25 + RRF fusion
├── RedisVectorRetriever.java # RediSearch HNSW vector index + FT.SEARCH
├── AstCompressor.java       # AST-aware context compression
└── RagIndexer.java          # CLI: index a codebase (chunk → embed → store)
```

### 6.2 Technology Mapping

| Python | Java |
|---|---|
| `tree-sitter` + `tree-sitter-python` | `io.github.tree-sitter:tree-sitter` + language JARs |
| `httpx.AsyncClient` → Ollama | `WebClient` → Ollama `/api/embeddings` |
| In-memory cosine similarity | Same (float[] dot product) |
| BM25 (custom) | Same algorithm (TF-IDF already in memory-server) |
| RediSearch `FT.SEARCH` via redis-py | RediSearch via Jedis/Lettuce `FT.SEARCH` |
| `asyncio.gather()` batch parallelism | `Flux.merge()` or `CompletableFuture.allOf()` |

### 6.3 Dependencies

```xml
<!-- tree-sitter Java bindings -->
<dependency>
    <groupId>io.github.tree-sitter</groupId>
    <artifactId>tree-sitter</artifactId>
    <version>0.24.0</version>
</dependency>

<!-- RediSearch (via Jedis) -->
<dependency>
    <groupId>redis.clients</groupId>
    <artifactId>jedis</artifactId>
</dependency>
```

### 6.4 TreeSitterChunker

```java
@Component
public class TreeSitterChunker {
    // Language registry: extension → TSLanguage
    private static final Map<String, TSLanguage> LANGUAGES = Map.of(
        ".py", new TSLanguage(TreeSitterPython.language()),
        ".java", new TSLanguage(TreeSitterJava.language()),
        ".js", new TSLanguage(TreeSitterJavaScript.language()),
        ".ts", new TSLanguage(TreeSitterTypeScript.language())
    );

    // Node types to extract per language
    private static final Map<String, Set<String>> CHUNK_TYPES = Map.of(
        ".py", Set.of("function_definition", "class_definition"),
        ".java", Set.of("method_declaration", "class_declaration", "constructor_declaration"),
        ".js", Set.of("function_declaration", "class_declaration", "method_definition"),
        ".ts", Set.of("function_declaration", "class_declaration", "method_definition")
    );

    public List<CodeChunk> chunkFile(Path filePath) {
        String ext = getExtension(filePath);
        TSLanguage lang = LANGUAGES.get(ext);
        if (lang == null) return List.of();

        byte[] source = Files.readAllBytes(filePath);
        try (var parser = new TSParser()) {
            parser.setLanguage(lang);
            try (var tree = parser.parse(source)) {
                return walkTree(tree.getRootNode(), source, filePath, ext);
            }
        }
    }
}
```

### 6.5 OllamaEmbedder

```java
@Component
public class OllamaEmbedder {
    private final WebClient webClient;
    private static final int MAX_EMBED_CHARS = 1500;
    private static final int EMBED_DIM = 1024;

    public Mono<float[]> embed(String text) {
        String cleaned = truncate(clean(text), MAX_EMBED_CHARS);
        return webClient.post()
            .uri("/api/embeddings")
            .bodyValue(Map.of("model", "mxbai-embed-large", "prompt", cleaned))
            .retrieve()
            .bodyToMono(EmbeddingResponse.class)
            .map(r -> r.embedding())
            .onErrorReturn(new float[EMBED_DIM]); // graceful: zero vector
    }

    public Flux<float[]> embedBatch(List<String> texts, int batchSize) {
        return Flux.fromIterable(texts)
            .buffer(batchSize)
            .concatMap(batch -> Flux.merge(batch.stream().map(this::embed).toList())
                .collectList()
                .delayElement(Duration.ofMillis(100)));
    }
}
```

---

## 7. What's NOT Ported (Out of Scope)

| Component | Reason |
|---|---|
| Learned reranker | ML model training in Python (scikit-learn) — not practical in Java without ONNX |
| Cross-encoder reranker | sentence-transformers dependency — use BM25+vector hybrid instead |
| Compression benchmark | Dev tooling, not runtime |
| RAG eval benchmark | Dev tooling, not runtime |

---

## 7. Dependencies (pom.xml)

```xml
<dependencies>
    <!-- Spring Boot -->
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-webflux</artifactId>
    </dependency>

    <!-- Spring AI — Ollama (default) -->
    <dependency>
        <groupId>org.springframework.ai</groupId>
        <artifactId>spring-ai-ollama-spring-boot-starter</artifactId>
    </dependency>

    <!-- Spring AI — OpenAI (optional) -->
    <dependency>
        <groupId>org.springframework.ai</groupId>
        <artifactId>spring-ai-openai-spring-boot-starter</artifactId>
    </dependency>

    <!-- Redis -->
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-data-redis-reactive</artifactId>
    </dependency>

    <!-- YAML parsing -->
    <dependency>
        <groupId>org.yaml</groupId>
        <artifactId>snakeyaml</artifactId>
    </dependency>

    <!-- Shared library -->
    <dependency>
        <groupId>com.example</groupId>
        <artifactId>mcp-common</artifactId>
        <version>${project.version}</version>
    </dependency>

    <!-- Test -->
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-test</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

---

## 8. API Contract (must match Python exactly)

```
POST /v1/chat/completions  — OpenAI-compatible, SSE streaming
GET  /v1/models            — List available models
GET  /health               — Health check
GET  /api/usage            — Token usage stats
POST /api/workspace/open   — Set workspace root
GET  /api/workspace/files  — File tree
GET  /api/workspace/file   — Read file content

SSE event types (must match Python):
  (default)    — OpenAI chat completion chunks
  tool_start   — {"tool":"file_read","input":{...},"seq":1}
  tool_end     — {"tool":"file_read","output":"...","seq":1}
  status       — {"state":"thinking"} / {"state":"complete","tool_count":3,"duration_ms":1200}

  Legacy (backward compat):
  Content chunks containing <!-- TOOL:{...} --> HTML comments
```

---

## 9. Effort Estimate

| Component | Effort | Complexity |
|---|---|---|
| Project scaffold (pom.xml, config, security) | 1h | Low |
| ChatController + SSE streaming | 3h | Medium |
| AgentService + AgentFactory (Spring AI) | 4h | High — Spring AI tool calling nuances |
| FileTools (read, write, edit, list, search) | 3h | Medium |
| GitTools (status, diff, commit) | 1h | Low |
| MemoryTools + CodeExecTools (REST clients) | 2h | Low — reuse McpRestClient pattern |
| PromptService (V1/V2, workspace context) | 1h | Low |
| IntentClassifier (meta, topic switch, complexity) | 1h | Low |
| ConversationStore (Redis) | 2h | Medium |
| SessionLane (Redis lock) | 1h | Low |
| AgentConfigLoader + ConfigWatcher | 2h | Medium |
| WorkspaceDetector | 1h | Low |
| ContextCompressor (Advisor) | 2h | Medium |
| RAG: TreeSitterChunker | 3h | High — tree-sitter Java SDK |
| RAG: OllamaEmbedder | 1h | Low |
| RAG: HybridRetriever (vector+BM25+RRF) | 3h | Medium |
| RAG: RedisVectorRetriever | 2h | Medium |
| RAG: AstCompressor | 2h | Medium |
| RAG: RagIndexer CLI | 1h | Low |
| WorkspaceController | 1h | Low |
| Tests (20-25 tests) | 4h | Medium |
| **Total** | **~40h** | |

---

## 10. File-by-File Implementation Plan

### Phase A: Scaffold + API (6 files)
1. `pom.xml` — dependencies
2. `AgentServerApplication.java` — main class
3. `application.yml` — configuration
4. `SecurityConfig.java` — extends McpSecurityConfigBase
5. `AgentProperties.java` — @ConfigurationProperties
6. `ChatRequest.java` / `ChatResponse.java` — request/response records

### Phase B: Tools (7 files)
7. `McpRestClient.java` — WebClient wrapper for backend calls
8. `FileTools.java` — 5 file tools
9. `GitTools.java` — 3 git tools
10. `MemoryTools.java` — 3 memory tools (REST → memory-server)
11. `CodeExecTools.java` — 2 code exec tools (REST → codeexec-server)
12. `RagSearchTool.java` — RAG search (Redis or REST)
13. `ToolRegistry.java` — name → FunctionCallback map

### Phase C: Agent Core (5 files)
14. `AgentFactory.java` — ChatClient per model, tool binding
15. `AgentService.java` — Agent loop, SSE streaming
16. `PromptService.java` — System prompts V1/V2 + workspace context
17. `IntentClassifier.java` — Meta-question, topic switch, complexity
18. `ContextCompressor.java` — Custom Advisor for context trimming

### Phase D: Infrastructure (5 files)
19. `ConversationStore.java` — Redis ChatMemory
20. `SessionLane.java` — Redis distributed lock
21. `WorkspaceDetector.java` — Project type detection
22. `SseEventBuilder.java` — Named SSE event helpers
23. `ChatController.java` — The main endpoint tying it all together

### Phase E: Config + Workspace (4 files)
24. `AgentConfig.java` — YAML config record
25. `AgentConfigLoader.java` — Load agents/*.yaml
26. `ConfigWatcher.java` — NIO WatchService
27. `WorkspaceController.java` — Workspace API

### Phase F: Tests (5 files)
28-32. Unit tests for IntentClassifier, FileTools, ContextCompressor, AgentConfigLoader, ChatController
