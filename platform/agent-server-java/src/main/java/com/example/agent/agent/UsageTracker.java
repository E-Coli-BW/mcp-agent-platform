package com.example.agent.agent;

import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * In-memory token usage tracker for cost monitoring.
 *
 * <p>Accumulates token usage across all requests for the lifecycle of the process.
 * Exposed via the /api/usage endpoint for operational monitoring.</p>
 */
@Component
public class UsageTracker {

    private final AtomicLong totalPromptTokens = new AtomicLong(0);
    private final AtomicLong totalCompletionTokens = new AtomicLong(0);
    private final AtomicInteger totalRequests = new AtomicInteger(0);
    private final AtomicInteger totalToolCalls = new AtomicInteger(0);
    private final AtomicLong totalDurationMs = new AtomicLong(0);

    /**
     * Record usage from a single request.
     */
    public void record(int promptTokens, int completionTokens, int toolCalls, long durationMs) {
        totalPromptTokens.addAndGet(promptTokens);
        totalCompletionTokens.addAndGet(completionTokens);
        totalRequests.incrementAndGet();
        totalToolCalls.addAndGet(toolCalls);
        totalDurationMs.addAndGet(durationMs);
    }

    /**
     * Get a summary of accumulated usage.
     */
    public Map<String, Object> getSummary() {
        int requests = totalRequests.get();
        long prompt = totalPromptTokens.get();
        long completion = totalCompletionTokens.get();
        long total = prompt + completion;
        long duration = totalDurationMs.get();

        return Map.of(
                "total_requests", requests,
                "total_prompt_tokens", prompt,
                "total_completion_tokens", completion,
                "total_tokens", total,
                "total_tool_calls", totalToolCalls.get(),
                "total_duration_ms", duration,
                "avg_tokens_per_request", requests > 0 ? total / requests : 0,
                "avg_duration_ms", requests > 0 ? duration / requests : 0
        );
    }
}

