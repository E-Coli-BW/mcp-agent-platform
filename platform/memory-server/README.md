# Memory MCP Server — Java HTTP (Long-Running) Version

## Why Java for HTTP MCP?

Pain points of the stdio version:
- Every client connection spawns a new JVM (346ms cold start)
- Each client holds its own process
- Cannot share an in-memory cache

Advantages of the HTTP version:
- **Start once, serve forever** — sub-5ms latency after JVM warm-up
- **Shared across clients** — VS Code, CLI, and Claude Desktop connect to the same port
- **In-memory cache** — ConcurrentHashMap avoids reading from disk every time
- **Full Spring Boot stack** — monitoring, security, rate limiting, and auditing out of the box
- **Virtual Threads (Java 21)** — high concurrency with zero pressure

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │  Spring Boot (port 8080)             │
                    │                                      │
  VS Code ──SSE──→  │  /sse endpoint (MCP over SSE)        │
  CLI     ──SSE──→  │                                      │
  Claude  ──SSE──→  │  ┌──────────────────────────┐       │
                    │  │  MemoryToolService        │       │
                    │  │  @Tool memory_set/get/... │       │
                    │  └────────────┬───────────────┘       │
                    │               ↓                       │
                    │  ConcurrentHashMap (in-memory cache)  │
                    │               ↓                       │
                    │  ~/.mcp-local/memory-store.json       │
                    │                                      │
                    │  ── Bonus: Spring Actuator ──        │
                    │  /actuator/health                    │
                    │  /actuator/metrics                   │
                    └──────────────────────────────────────┘
```

## Quick Start

```bash
cd compare/java-http
mvn spring-boot:run
```

Server starts at `http://localhost:8080`.

## Client Configuration

### VS Code (.vscode/mcp.json)
```json
{
  "servers": {
    "memory-store": {
      "type": "sse",
      "url": "http://localhost:8080/sse"
    }
  }
}
```

### Copilot CLI (~/.copilot/mcp-config.json)
Copilot CLI currently only supports stdio, not SSE.
You'd need a thin stdio-to-HTTP proxy, or wait for SSE support.

### Claude Desktop
```json
{
  "mcpServers": {
    "memory-store": {
      "url": "http://localhost:8080/sse"
    }
  }
}
```

## Key Differences from stdio Version

| Aspect | stdio (Java) | HTTP (Java + Spring) |
|--------|-------------|---------------------|
| Startup cost | 346ms per connection | 346ms once, then ~0 |
| Concurrent clients | 1 per process | Unlimited |
| Memory | Load from disk every call | In-memory ConcurrentHashMap |
| Monitoring | None | /actuator/health, /metrics |
| Auth | None | Spring Security (add if needed) |
| Deployment | `java -jar` | `java -jar` or Docker |
| Latency (warm) | ~10ms | **<5ms** (no disk read) |

## When to Use This

✅ **Team setting** — multiple developers share one memory server
✅ **Enterprise** — need auth, audit, rate limiting
✅ **High throughput** — many agents calling tools concurrently
✅ **Monitoring** — need health checks and metrics

❌ **Personal use** — overkill, stdio TypeScript version is simpler
❌ **Copilot CLI** — doesn't support SSE yet (stdio only)
