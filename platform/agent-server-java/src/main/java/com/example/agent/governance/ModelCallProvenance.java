package com.example.agent.governance;

import java.time.Instant;
import java.util.Map;

/**
 * Reproducibility-grade metadata emitted per model call.
 */
public record ModelCallProvenance(
        String eventId,
        String runId,
        String requestId,
        String traceId,
        String tenantId,
        String sessionId,
        String runtime,
        String callSite,
        String provider,
        String model,
        Double temperature,
        Integer maxTokens,
        String promptId,
        String promptVersion,
        String promptHash,
        Map<String, Object> featureFlags,
        int promptTokens,
        int completionTokens,
        int durationMs,
        int fallbackCount,
        int retryCount,
        String status,
        String errorClass,
        Instant timestamp
) {
}

