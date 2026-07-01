package com.example.completion.metrics;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.springframework.stereotype.Component;

import java.time.Duration;

/**
 * Completion-specific metrics exposed to Prometheus.
 *
 * Key metrics:
 * - completion_ttft: Time to First Token (P50/P95/P99) — most important for UX
 * - completion_tokens_total: Total tokens generated
 * - completion_requests_total: Request count by status (hit/miss/error/cancelled)
 */
@Component
public class CompletionMetrics {

    private final Timer ttftTimer;
    private final Counter tokensCounter;
    private final Counter requestsTotal;
    private final Counter cacheHitsCounter;
    private final Counter cacheMissCounter;
    private final Counter cancelledCounter;

    public CompletionMetrics(MeterRegistry registry) {
        this.ttftTimer = Timer.builder("completion.ttft")
                .description("Time to first token")
                .publishPercentiles(0.5, 0.95, 0.99)
                .register(registry);

        this.tokensCounter = Counter.builder("completion.tokens.total")
                .description("Total completion tokens generated")
                .register(registry);

        this.requestsTotal = Counter.builder("completion.requests.total")
                .tag("status", "completed")
                .description("Total completion requests")
                .register(registry);

        this.cacheHitsCounter = Counter.builder("completion.requests.total")
                .tag("status", "cache_hit")
                .register(registry);

        this.cacheMissCounter = Counter.builder("completion.requests.total")
                .tag("status", "cache_miss")
                .register(registry);

        this.cancelledCounter = Counter.builder("completion.requests.total")
                .tag("status", "cancelled")
                .register(registry);
    }

    public void recordTtft(Duration duration) {
        ttftTimer.record(duration);
    }

    public void recordTokens(int count) {
        tokensCounter.increment(count);
    }

    public void recordCompleted() {
        requestsTotal.increment();
    }

    public void recordCacheHit() {
        cacheHitsCounter.increment();
    }

    public void recordCacheMiss() {
        cacheMissCounter.increment();
    }

    public void recordCancelled() {
        cancelledCounter.increment();
    }
}
