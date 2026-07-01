package com.example.auth.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * Redis-backed distributed rate limiter using sorted set sliding window.
 *
 * Algorithm:
 * 1. Remove expired entries (ZREMRANGEBYSCORE)
 * 2. Count remaining entries (ZCARD)
 * 3. If under limit, add new entry (ZADD) with score = current timestamp
 * 4. Set TTL on the key (EXPIRE)
 *
 * All operations are atomic via Lua script — no race conditions.
 *
 * Graceful degradation: if Redis is unavailable, allows the request
 * (falls through to in-memory rate limiter as backup).
 */
@Service
public class RedisRateLimiter {

    private static final Logger log = LoggerFactory.getLogger(RedisRateLimiter.class);

    private static final String RATE_LIMIT_SCRIPT = """
            local key = KEYS[1]
            local limit = tonumber(ARGV[1])
            local window = tonumber(ARGV[2])
            local now = tonumber(ARGV[3])
            redis.call('ZREMRANGEBYSCORE', key, 0, now - window * 1000)
            local count = redis.call('ZCARD', key)
            if count < limit then
                redis.call('ZADD', key, now, now .. '-' .. math.random(1000000))
                redis.call('PEXPIRE', key, window * 1000)
                return 1
            end
            return 0
            """;

    private final StringRedisTemplate redisTemplate;
    private final DefaultRedisScript<Long> rateLimitScript;
    private final int maxAttempts;
    private final int windowSeconds;

    public RedisRateLimiter(
            ObjectProvider<StringRedisTemplate> redisTemplateProvider,
            @Value("${auth.rate-limit.login.max-attempts:20}") int maxAttempts,
            @Value("${auth.rate-limit.login.window-seconds:900}") int windowSeconds) {
        this.redisTemplate = redisTemplateProvider.getIfAvailable();
        this.maxAttempts = maxAttempts;
        this.windowSeconds = windowSeconds;

        this.rateLimitScript = new DefaultRedisScript<>();
        this.rateLimitScript.setScriptText(RATE_LIMIT_SCRIPT);
        this.rateLimitScript.setResultType(Long.class);
    }

    /**
     * Check and record a login attempt.
     *
     * @param key rate limit key (e.g., "rate:login:ip:192.168.1.1" or "rate:login:user:alice")
     * @return true if the attempt is ALLOWED, false if rate limited
     */
    public boolean tryAcquire(String key) {
        if (redisTemplate == null) {
            return true;
        }
        try {
            Long result = redisTemplate.execute(
                    rateLimitScript,
                    List.of(key),
                    String.valueOf(maxAttempts),
                    String.valueOf(windowSeconds),
                    String.valueOf(System.currentTimeMillis())
            );
            return result != null && result == 1L;
        } catch (Exception e) {
            log.warn("Redis rate limiter unavailable, allowing request: key={}, error={}", key, e.getMessage());
            return true; // degrade to allow
        }
    }

    /**
     * Clear rate limit for a key (e.g., on successful login).
     */
    public void reset(String key) {
        if (redisTemplate == null) {
            return;
        }
        try {
            redisTemplate.delete(key);
        } catch (Exception e) {
            log.warn("Failed to reset rate limit: key={}, error={}", key, e.getMessage());
        }
    }

    /**
     * Check remaining attempts without consuming one.
     */
    public long remainingAttempts(String key) {
        if (redisTemplate == null) {
            return maxAttempts;
        }
        try {
            Long count = redisTemplate.opsForZSet().zCard(key);
            return Math.max(0, maxAttempts - (count != null ? count : 0));
        } catch (Exception e) {
            return maxAttempts; // degrade: assume full budget
        }
    }
}
