package com.example.auth.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.Instant;

/**
 * Redis-backed token blacklist for immediate JWT revocation on logout.
 *
 * Design:
 * - Key: token:blacklist:{jti}
 * - Value: "1"
 * - TTL: remaining time until token expiry (auto-cleanup)
 *
 * Graceful degradation: if Redis is unavailable, blacklist operations
 * are skipped (log + continue). Access tokens have short TTL (1h),
 * so max exposure window is bounded.
 */
@Service
public class TokenBlacklistService {

    private static final Logger log = LoggerFactory.getLogger(TokenBlacklistService.class);
    private static final String KEY_PREFIX = "token:blacklist:";

    private final StringRedisTemplate redisTemplate;

    public TokenBlacklistService(ObjectProvider<StringRedisTemplate> redisTemplateProvider) {
        this.redisTemplate = redisTemplateProvider.getIfAvailable();
    }

    /**
     * Blacklist a JWT by its JTI claim. The entry auto-expires when the token would have expired.
     *
     * @param jti       the JWT ID (unique per token)
     * @param expiresAt when the token expires (used to calculate TTL)
     */
    public void blacklist(String jti, Instant expiresAt) {
        if (redisTemplate == null) {
            return;
        }
        try {
            Duration ttl = Duration.between(Instant.now(), expiresAt);
            if (ttl.isNegative() || ttl.isZero()) return; // already expired
            redisTemplate.opsForValue().set(KEY_PREFIX + jti, "1", ttl);
            log.debug("Blacklisted token: jti={}, ttl={}s", jti, ttl.toSeconds());
        } catch (Exception e) {
            log.warn("Failed to blacklist token (Redis unavailable): jti={}, error={}", jti, e.getMessage());
        }
    }

    /**
     * Check if a token is blacklisted. Used by downstream services via /auth/check-blacklist.
     *
     * @return true if the token is blacklisted (should be rejected)
     */
    public boolean isBlacklisted(String jti) {
        if (redisTemplate == null) {
            return false;
        }
        try {
            return Boolean.TRUE.equals(redisTemplate.hasKey(KEY_PREFIX + jti));
        } catch (Exception e) {
            log.warn("Failed to check blacklist (Redis unavailable): jti={}, error={}", jti, e.getMessage());
            // Degrade to allow — prefer availability over strict revocation
            return false;
        }
    }
}
