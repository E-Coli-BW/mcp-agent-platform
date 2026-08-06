# Code Completion Service (FIM) — Architecture Design

> Fill-in-the-Middle completion for IDE integration. Not yet implemented — this is the design.

---

## What It Does

The code completion service provides real-time code suggestions as the user types, similar to GitHub Copilot. It uses FIM (Fill-in-the-Middle) prompting to generate contextually appropriate completions.

```
User types:
    def calculate_total(items):
        total = 0
        for item in items:
            |  ← cursor here
        return total

Service returns:
    total += item.price * item.quantity
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     COMPLETION SERVICE                            │
│                     (Java Spring WebFlux)                         │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Controller   │  │ FIM Builder  │  │ Streaming Proxy      │   │
│  │ POST /v1/    │→ │ prefix/      │→ │ WebClient + Flux     │   │
│  │ completions  │  │ suffix       │  │ SSE to Ollama/       │   │
│  │              │  │ extraction   │  │ DeepSeek/OpenAI      │   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│         ↑                                      ↓                 │
│  ┌──────┴──────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ Debounce    │  │ Prefix Cache │  │ Cost Tracker         │   │
│  │ Filter      │  │ (Caffeine)   │  │ (Micrometer)         │   │
│  │ cancel prev │  │ hash(prefix) │  │ TTFT, tokens, $      │   │
│  └─────────────┘  └──────────────┘  └──────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

## API Design

### POST /v1/completions

```json
// Request (OpenAI-compatible)
{
    "model": "deepseek-coder",
    "prompt": "<|fim_begin|>def calc(items):\n    total = 0\n    for item in items:\n        <|fim_hole|>\n    return total<|fim_end|>",
    "max_tokens": 128,
    "temperature": 0.2,
    "stream": true,
    "stop": ["\n\n", "def ", "class "]
}

// Response (SSE stream)
data: {"choices":[{"text":"total += item.price * item.quantity","finish_reason":null}]}
data: {"choices":[{"text":"","finish_reason":"stop"}]}
data: [DONE]
```

### Alternative: Raw cursor position API

```json
// Request (IDE-friendly)
{
    "file_content": "def calc(items):\n    total = 0\n    for item in items:\n        \n    return total",
    "cursor_line": 4,
    "cursor_column": 8,
    "language": "python",
    "file_path": "src/utils.py",
    "max_tokens": 128
}

// The service constructs FIM prompt internally
```

## Key Components

### 1. FIM Format Builder

Different models use different FIM tokens:

```java
public interface FimFormatter {
    String format(String prefix, String suffix);
}

// DeepSeek Coder
class DeepSeekFimFormatter implements FimFormatter {
    public String format(String prefix, String suffix) {
        return "<|fim_begin|>" + prefix + "<|fim_hole|>" + suffix + "<|fim_end|>";
    }
}

// CodeLlama / StarCoder
class CodeLlamaFimFormatter implements FimFormatter {
    public String format(String prefix, String suffix) {
        return "<PRE> " + prefix + " <SUF>" + suffix + " <MID>";
    }
}

// OpenAI (suffix parameter, not in-prompt)
class OpenAiFimFormatter implements FimFormatter {
    // Uses API suffix parameter instead of in-prompt tokens
}
```

### 2. Debounce Filter (WebFilter)

```java
@Component
public class CompletionDebounceFilter implements WebFilter {
    // Map<requestId, Disposable> to cancel previous in-flight requests
    private final ConcurrentHashMap<String, Disposable> inflight = new ConcurrentHashMap<>();

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String requestId = exchange.getRequest().getHeaders().getFirst("X-Request-Id");
        if (requestId != null) {
            Disposable prev = inflight.put(requestId, /* new subscription */);
            if (prev != null) prev.dispose(); // cancel previous
        }
        return chain.filter(exchange);
    }
}
```

### 3. Prefix Cache (Caffeine)

```java
@Service
public class CompletionCacheService {
    private final Cache<String, String> cache = Caffeine.newBuilder()
        .maximumSize(1000)
        .expireAfterWrite(Duration.ofMinutes(5))
        .build();

    public String getCacheKey(String prefix, String model) {
        // Hash last 10 lines of prefix + model
        String tail = lastNLines(prefix, 10);
        return DigestUtils.sha256Hex(tail + ":" + model);
    }

    public Optional<String> get(String prefix, String model) {
        return Optional.ofNullable(cache.getIfPresent(getCacheKey(prefix, model)));
    }
}
```

### 4. TTFT Metrics (Micrometer)

```java
@Service
public class CompletionMetrics {
    private final Timer ttftTimer;       // Time to first token
    private final Counter tokenCounter;  // Total completion tokens
    private final Counter cacheHits;     // Cache hit ratio

    public CompletionMetrics(MeterRegistry registry) {
        this.ttftTimer = Timer.builder("completion.ttft")
            .tag("model", "unknown")
            .publishPercentiles(0.5, 0.95, 0.99)
            .register(registry);
    }
}
```

## Context Window Strategy for Completions

IDE completion context is different from chat — we need to be smart about what goes into the prompt:

```
Priority 1: Current file (cursor ±50 lines)        ~2K tokens
Priority 2: Open file tabs in editor                ~1K tokens
Priority 3: Import statements (dependency graph)    ~500 tokens
Priority 4: Recently edited files                   ~500 tokens
────────────────────────────────────────────────────
Total budget: ~4K tokens (fast inference)
```

## Performance Targets

| Metric | Target | Why |
|--------|--------|-----|
| TTFT (time to first token) | P95 < 200ms | User perceives <200ms as instant |
| Full completion | P95 < 500ms | Before user types next char |
| Cache hit rate | >30% | Repeated patterns in same file |
| Request cancellation | <10ms | Debounce must be fast |

## Integration with Agent Server

The completion service is separate from the agent server:
- **Agent** = slow, multi-step, uses many tools (seconds)
- **Completion** = fast, single-shot, no tools (milliseconds)

```
Monaco IDE
├── Chat Panel → Agent Server (port 8500)  ← slow, smart
└── Editor → Completion Service (port 8600) ← fast, simple
```

## Implementation Plan

| Step | What | Effort |
|------|------|--------|
| 1 | Spring WebFlux skeleton + POST /v1/completions | 0.5d |
| 2 | FIM format builder (DeepSeek + CodeLlama) | 0.5d |
| 3 | WebClient streaming proxy to Ollama | 0.5d |
| 4 | Debounce filter (cancel previous requests) | 0.5d |
| 5 | Caffeine prefix cache | 0.5d |
| 6 | Micrometer TTFT metrics | 0.5d |
| 7 | Monaco integration (editor.InlineCompletionProvider) | 1d |
| **Total** | | **4d** |
