package com.example.completion.cache;

import com.example.completion.config.CompletionProperties;
import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.binder.cache.CaffeineCacheMetrics;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.HexFormat;
import java.util.Optional;

/**
 * Caffeine-based prefix cache for code completions.
 * 
 * Key: SHA-256 hash of (last N lines of prefix + model name)
 * Value: completion text
 * 
 * Cache hit means we can skip the LLM call entirely — TTFT = ~0ms.
 */
@Component
public class CompletionCache {

    private static final Logger log = LoggerFactory.getLogger(CompletionCache.class);
    private static final int PREFIX_TAIL_LINES = 10; // Hash last 10 lines for key

    private final Cache<String, String> cache;

    public CompletionCache(CompletionProperties props, MeterRegistry meterRegistry) {
        this.cache = Caffeine.newBuilder()
                .maximumSize(props.getCache().getMaxSize())
                .expireAfterWrite(Duration.ofMinutes(props.getCache().getExpireMinutes()))
                .recordStats()
                .build();

        // Expose cache metrics to Prometheus
        CaffeineCacheMetrics.monitor(meterRegistry, cache, "completion_cache");

        log.info("Completion cache initialized: maxSize={}, expireMinutes={}",
                props.getCache().getMaxSize(), props.getCache().getExpireMinutes());
    }

    public Optional<String> get(String prefix, String model) {
        String key = buildKey(prefix, model);
        String cached = cache.getIfPresent(key);
        if (cached != null) {
            log.debug("Cache HIT for model={}", model);
        }
        return Optional.ofNullable(cached);
    }

    public void put(String prefix, String model, String completion) {
        String key = buildKey(prefix, model);
        cache.put(key, completion);
    }

    /**
     * Build cache key: SHA-256(last N lines of prefix + ":" + model).
     * We only hash the tail of the prefix because the beginning (imports, class declaration)
     * changes rarely — the cursor position matters most.
     */
    String buildKey(String prefix, String model) {
        String tail = lastNLines(prefix, PREFIX_TAIL_LINES);
        String raw = tail + ":" + model;
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] hash = md.digest(raw.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash);
        } catch (Exception e) {
            // Fallback: use raw string hash
            return String.valueOf(raw.hashCode());
        }
    }

    static String lastNLines(String text, int n) {
        String[] lines = text.split("\n", -1);
        if (lines.length <= n) return text;
        return String.join("\n", java.util.Arrays.copyOfRange(lines, lines.length - n, lines.length));
    }
}
