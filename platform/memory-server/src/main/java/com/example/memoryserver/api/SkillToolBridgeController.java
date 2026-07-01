package com.example.memoryserver.api;

import com.example.memoryserver.tool.SkillToolService;
import io.github.resilience4j.circuitbreaker.annotation.CircuitBreaker;
import io.github.resilience4j.ratelimiter.annotation.RateLimiter;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

import static com.example.mcp.common.security.ToolBridgeSupport.execute;

/**
 * REST bridge for agent server → skill tool calls.
 * Same pattern as ToolBridgeController but for skill-specific operations.
 */
@RestController
@RequestMapping("/api/tools")
public class SkillToolBridgeController {

    private final SkillToolService toolService;

    public SkillToolBridgeController(SkillToolService toolService) {
        this.toolService = toolService;
    }

    @PostMapping("/skill_set")
    @RateLimiter(name = "toolBridge", fallbackMethod = "rateLimitFallback")
    @CircuitBreaker(name = "memoryService", fallbackMethod = "circuitBreakerFallback")
    @SuppressWarnings("unchecked")
    public Map<String, String> skillSet(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.skill_set(
                (String) params.get("key"),
                (String) params.get("title"),
                (String) params.get("problem"),
                params.get("steps"),
                (String) params.getOrDefault("category", null),
                (String) params.getOrDefault("trigger_patterns", null),
                (String) params.getOrDefault("trigger_tools", null),
                (String) params.getOrDefault("trigger_errors", null),
                (List<String>) params.getOrDefault("tags", null)));
    }

    @PostMapping("/skill_get")
    public Map<String, String> skillGet(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.skill_get(
                (String) params.get("key"),
                params.containsKey("version") ? ((Number) params.get("version")).intValue() : null));
    }

    @PostMapping("/skill_list")
    @SuppressWarnings("unchecked")
    public Map<String, String> skillList(@RequestBody(required = false) Map<String, Object> params) {
        return execute(() -> {
            var p = params != null ? params : Map.<String, Object>of();
            return toolService.skill_list(
                    (String) p.getOrDefault("category", null),
                    (List<String>) p.getOrDefault("tags", null));
        });
    }

    @PostMapping("/skill_history")
    public Map<String, String> skillHistory(@RequestBody Map<String, String> params) {
        return execute(() -> toolService.skill_history(params.get("key")));
    }

    @PostMapping("/skill_rollback")
    public Map<String, String> skillRollback(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.skill_rollback(
                (String) params.get("key"),
                ((Number) params.get("version")).intValue()));
    }

    @PostMapping("/skill_feedback")
    public Map<String, String> skillFeedback(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.skill_feedback(
                (String) params.get("key"),
                (Boolean) params.get("success")));
    }

    @GetMapping("/skill_triggers")
    public Map<String, String> skillTriggers() {
        return execute(() -> toolService.skill_triggers());
    }

    // ── Resilience4j fallbacks ───────────────────────────────────

    private Map<String, String> rateLimitFallback(Map<String, ?> params, io.github.resilience4j.ratelimiter.RequestNotPermitted ex) {
        return Map.of("result", "❌ Rate limit exceeded. Please slow down.");
    }

    private Map<String, String> circuitBreakerFallback(Map<String, ?> params, io.github.resilience4j.circuitbreaker.CallNotPermittedException ex) {
        return Map.of("result", "❌ Service temporarily unavailable (circuit breaker open).");
    }
}
