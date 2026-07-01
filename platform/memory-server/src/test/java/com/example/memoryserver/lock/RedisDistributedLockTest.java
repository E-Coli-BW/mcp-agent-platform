package com.example.memoryserver.lock;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;
import org.springframework.data.redis.core.script.RedisScript;

import java.time.Duration;
import java.util.Collections;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class RedisDistributedLockTest {

    private StringRedisTemplate redis;
    private ValueOperations<String, String> valueOps;
    private RedisDistributedLock lock;

    @BeforeEach
    void setUp() {
        redis = mock(StringRedisTemplate.class);
        valueOps = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(valueOps);
        lock = new RedisDistributedLock(redis);
    }

    @Test
    void acquire_succeeds_when_key_not_exists() {
        when(valueOps.setIfAbsent(anyString(), anyString(), any(Duration.class)))
                .thenReturn(true);

        String token = lock.acquire("resource:1", Duration.ofSeconds(10));

        assertNotNull(token);
        verify(valueOps).setIfAbsent(eq("lock:resource:1"), eq(token), eq(Duration.ofSeconds(10)));
    }

    @Test
    void acquire_returns_null_when_already_locked() {
        when(valueOps.setIfAbsent(anyString(), anyString(), any(Duration.class)))
                .thenReturn(false);

        String token = lock.acquire("resource:1", Duration.ofSeconds(10));

        assertNull(token);
    }

    @Test
    void release_succeeds_with_correct_token() {
        when(redis.execute(any(RedisScript.class), anyList(), anyString()))
                .thenReturn(1L);

        boolean released = lock.release("resource:1", "my-token");

        assertTrue(released);
    }

    @Test
    void release_fails_with_wrong_token() {
        when(redis.execute(any(RedisScript.class), anyList(), anyString()))
                .thenReturn(0L);

        boolean released = lock.release("resource:1", "wrong-token");

        assertFalse(released);
    }

    @Test
    void acquireWithRetry_succeeds_on_second_attempt() {
        when(valueOps.setIfAbsent(anyString(), anyString(), any(Duration.class)))
                .thenReturn(false)  // first attempt fails
                .thenReturn(true);  // second attempt succeeds

        String token = lock.acquireWithRetry("resource:1", Duration.ofSeconds(10), 2, Duration.ofMillis(10));

        assertNotNull(token);
    }

    @Test
    void acquireWithRetry_returns_null_after_max_retries() {
        when(valueOps.setIfAbsent(anyString(), anyString(), any(Duration.class)))
                .thenReturn(false);

        String token = lock.acquireWithRetry("resource:1", Duration.ofSeconds(10), 2, Duration.ofMillis(10));

        assertNull(token);
    }

    @Test
    void isLocked_returns_true_when_key_exists() {
        when(redis.hasKey("lock:resource:1")).thenReturn(true);

        assertTrue(lock.isLocked("resource:1"));
    }

    @Test
    void isLocked_returns_false_when_key_not_exists() {
        when(redis.hasKey("lock:resource:1")).thenReturn(false);

        assertFalse(lock.isLocked("resource:1"));
    }
}
