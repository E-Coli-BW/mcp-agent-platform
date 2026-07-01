package com.example.memoryserver.api;

import com.example.memoryserver.tool.MemoryToolService;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.ratelimiter.annotation.RateLimiter;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

import static com.example.mcp.common.security.ToolBridgeSupport.execute;

/**
 * REST bridge for agent server → memory tool calls.
 *
 * <p>Resilience4j annotations provide rate limiting and circuit breaking.
 * Tenant lifecycle managed by {@link com.example.mcp.common.security.ToolBridgeSupport}.</p>
 */
@RestController
@RequestMapping("/api/tools")
public class ToolBridgeController {

    private final MemoryToolService toolService;

    public ToolBridgeController(MemoryToolService toolService) {
        this.toolService = toolService;
    }

    // ── Resilience4j fallbacks ───────────────────────────────────

    private Map<String, String> rateLimitFallback(Map<String, ?> params, io.github.resilience4j.ratelimiter.RequestNotPermitted ex) {
        return Map.of("result", "❌ Rate limit exceeded. Please slow down (max 50 req/s).");
    }

    private Map<String, String> circuitBreakerFallback(Map<String, ?> params, CallNotPermittedException ex) {
        return Map.of("result", "❌ Service temporarily unavailable (circuit breaker open). Retry in 10s.");
    }

    private Map<String, String> circuitBreakerFallbackNoArgs(CallNotPermittedException ex) {
        return Map.of("result", "❌ Service temporarily unavailable (circuit breaker open). Retry in 10s.");
    }

    // ── Tool endpoints ──────────────────────────────────────────

    @PostMapping("/memory_set")
    @RateLimiter(name = "toolBridge", fallbackMethod = "rateLimitFallback")
    @CircuitBreaker(name = "memoryService", fallbackMethod = "circuitBreakerFallback")
    @SuppressWarnings("unchecked")
    public Map<String, String> memorySet(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.memory_set(
                (String) params.get("key"),
                (String) params.get("content"),
                (String) params.getOrDefault("namespace", null),
                (List<String>) params.getOrDefault("tags", null),
                (Boolean) params.getOrDefault("pinned", null)));
    }

    @PostMapping("/memory_get")
    public Map<String, String> memoryGet(@RequestBody Map<String, String> params) {
        return execute(() -> toolService.memory_get(params.get("key")));
    }

    @PostMapping("/memory_search")
    @RateLimiter(name = "toolBridge", fallbackMethod = "rateLimitFallback")
    @CircuitBreaker(name = "memoryService", fallbackMethod = "circuitBreakerFallback")
    @SuppressWarnings("unchecked")
    public Map<String, String> memorySearch(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.memory_search(
                (String) params.get("query"),
                (String) params.getOrDefault("namespace", null),
                (List<String>) params.getOrDefault("tags", null),
                params.containsKey("limit") ? ((Number) params.get("limit")).intValue() : null));
    }

    @PostMapping("/memory_delete")
    public Map<String, String> memoryDelete(@RequestBody Map<String, String> params) {
        return execute(() -> toolService.memory_delete(params.get("key")));
    }

    @PostMapping("/memory_context")
    @CircuitBreaker(name = "memoryService", fallbackMethod = "circuitBreakerFallbackNoArgs")
    public Map<String, String> memoryContext() {
        return execute(() -> toolService.memory_context());
    }

    @PostMapping("/memory_list")
    @SuppressWarnings("unchecked")
    public Map<String, String> memoryList(@RequestBody(required = false) Map<String, Object> params) {
        return execute(() -> {
            var p = params != null ? params : Map.<String, Object>of();
            return toolService.memory_list(
                    (String) p.getOrDefault("namespace", null),
                    (List<String>) p.getOrDefault("tags", null));
        });
    }

    @PostMapping("/memory_pin")
    public Map<String, String> memoryPin(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.memory_pin(
                (String) params.get("key"),
                (Boolean) params.getOrDefault("pinned", null)));
    }
}
