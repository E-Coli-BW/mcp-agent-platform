package com.example.memoryserver.lock;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnBean;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Collections;
import java.util.UUID;

/**
 * Redis-based distributed lock implementation.
 *
 * Uses SET NX EX for atomic acquire, Lua script for atomic release.
 * This prevents the classic "delete someone else's lock" race condition.
 *
 * Design notes:
 * - UUID as value prevents releasing another client's lock
 * - Lua for release ensures atomic check-and-delete (no race between GET and DEL)
 * - EX (TTL) prevents deadlock if holder crashes
 * - Production upgrade path: Redisson RedLock for multi-node Redis
 *
 * Usage:
 *   String token = lock.acquire("resource:123", Duration.ofSeconds(10));
 *   try {
 *       // critical section
 *   } finally {
 *       lock.release("resource:123", token);
 *   }
 */
@ConditionalOnBean(StringRedisTemplate.class)
@Component
public class RedisDistributedLock {

    private static final Logger log = LoggerFactory.getLogger(RedisDistributedLock.class);

    private static final String LOCK_PREFIX = "lock:";

    // Lua script for atomic release: check value matches, then delete
    // This prevents releasing a lock held by another client
    private static final String RELEASE_SCRIPT =
            "if redis.call('get', KEYS[1]) == ARGV[1] then " +
            "  return redis.call('del', KEYS[1]) " +
            "else " +
            "  return 0 " +
            "end";

    private final StringRedisTemplate redis;

    public RedisDistributedLock(StringRedisTemplate redis) {
        this.redis = redis;
    }

    /**
     * Try to acquire a distributed lock.
     *
     * @param resourceKey the resource to lock (e.g., "memory:tenant1:key123")
     * @param ttl lock expiration time (prevents deadlock if holder crashes)
     * @return lock token (UUID) if acquired, null if already held by another client
     */
    public String acquire(String resourceKey, Duration ttl) {
        String key = LOCK_PREFIX + resourceKey;
        String token = UUID.randomUUID().toString();

        // SET key token NX EX ttl — atomic: set only if not exists, with expiration
        Boolean acquired = redis.opsForValue().setIfAbsent(key, token, ttl);

        if (Boolean.TRUE.equals(acquired)) {
            log.debug("Lock acquired: key={}, token={}, ttl={}s", key, token, ttl.getSeconds());
            return token;
        }
        log.debug("Lock NOT acquired (held by another): key={}", key);
        return null;
    }

    /**
     * Try to acquire with retry.
     *
     * @param resourceKey the resource to lock
     * @param ttl lock TTL
     * @param maxRetries max number of retries
     * @param retryDelay delay between retries
     * @return lock token if acquired, null if all retries exhausted
     */
    public String acquireWithRetry(String resourceKey, Duration ttl, int maxRetries, Duration retryDelay) {
        for (int i = 0; i <= maxRetries; i++) {
            String token = acquire(resourceKey, ttl);
            if (token != null) return token;
            if (i < maxRetries) {
                try {
                    Thread.sleep(retryDelay.toMillis());
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return null;
                }
            }
        }
        return null;
    }

    /**
     * Release a distributed lock. Only succeeds if the token matches (you own the lock).
     *
     * @param resourceKey the resource to unlock
     * @param token the token returned by acquire()
     * @return true if successfully released, false if lock was already expired or held by another
     */
    public boolean release(String resourceKey, String token) {
        String key = LOCK_PREFIX + resourceKey;

        // Execute Lua script atomically: check token matches, then delete
        DefaultRedisScript<Long> script = new DefaultRedisScript<>(RELEASE_SCRIPT, Long.class);
        Long result = redis.execute(script, Collections.singletonList(key), token);

        boolean released = result != null && result == 1L;
        if (released) {
            log.debug("Lock released: key={}", key);
        } else {
            log.warn("Lock release failed (expired or held by another): key={}", key);
        }
        return released;
    }

    /**
     * Check if a resource is currently locked (without acquiring).
     */
    public boolean isLocked(String resourceKey) {
        return Boolean.TRUE.equals(redis.hasKey(LOCK_PREFIX + resourceKey));
    }
}
