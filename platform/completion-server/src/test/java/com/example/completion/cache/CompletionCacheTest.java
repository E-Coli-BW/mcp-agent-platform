package com.example.completion.cache;

import com.example.completion.config.CompletionProperties;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class CompletionCacheTest {

    private CompletionCache cache;

    @BeforeEach
    void setUp() {
        CompletionProperties props = new CompletionProperties();
        props.getCache().setMaxSize(100);
        props.getCache().setExpireMinutes(5);
        cache = new CompletionCache(props, new SimpleMeterRegistry());
    }

    @Test
    void putAndGet() {
        cache.put("def hello():\n    ", "qwen", "return 'world'");
        var result = cache.get("def hello():\n    ", "qwen");
        assertTrue(result.isPresent());
        assertEquals("return 'world'", result.get());
    }

    @Test
    void missOnDifferentPrefix() {
        cache.put("prefix_a", "model", "completion_a");
        var result = cache.get("prefix_b", "model");
        assertTrue(result.isEmpty());
    }

    @Test
    void missOnDifferentModel() {
        cache.put("same_prefix", "model_a", "completion_a");
        var result = cache.get("same_prefix", "model_b");
        assertTrue(result.isEmpty());
    }

    @Test
    void lastNLinesHelper() {
        String text = "a\nb\nc\nd\ne";
        assertEquals("d\ne", CompletionCache.lastNLines(text, 2));
        assertEquals("a\nb\nc\nd\ne", CompletionCache.lastNLines(text, 10)); // fewer lines than N
    }

    @Test
    void keyIsDeterministic() {
        String key1 = cache.buildKey("hello world", "qwen");
        String key2 = cache.buildKey("hello world", "qwen");
        assertEquals(key1, key2);
    }
}
