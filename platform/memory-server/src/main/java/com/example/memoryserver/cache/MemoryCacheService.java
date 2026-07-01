package com.example.memoryserver.cache;

import com.example.memoryserver.model.MemoryEntity;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnBean;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Optional;

/**
 * Redis cache layer for memory entries.
 * Strategy: read-through, write-invalidate.
 *
 * Graceful degradation: if Redis is down, all methods return empty/no-op
 * and the system falls back to DB-only mode.
 */
@ConditionalOnBean(StringRedisTemplate.class)
@Component
public class MemoryCacheService implements MemoryCache {

    private static final Logger log = LoggerFactory.getLogger(MemoryCacheService.class);
    private static final Duration ENTRY_TTL = Duration.ofHours(1);
    private static final Duration CONTEXT_TTL = Duration.ofSeconds(30);

    private final StringRedisTemplate redis;
    private final ObjectMapper mapper;

    public MemoryCacheService(StringRedisTemplate redis, ObjectMapper mapper) {
        this.redis = redis;
        this.mapper = mapper;
    }

    // ── Key Builders ─────────────────────────────────────────────

    private String entryKey(String tenantId, String key) {
        return "memory:" + tenantId + ":" + key;
    }

    private String contextKey(String tenantId) {
        return "memory:ctx:" + tenantId;
    }

    // ── Entry Cache ──────────────────────────────────────────────

    /** Get cached entry, or empty if miss/error. */
    public Optional<MemoryEntity> getEntry(String tenantId, String key) {
        try {
            String json = redis.opsForValue().get(entryKey(tenantId, key));
            if (json == null) return Optional.empty();
            return Optional.of(mapper.readValue(json, MemoryEntity.class));
        } catch (Exception e) {
            log.warn("Cache read failed for {}:{}, falling back to DB", tenantId, key, e);
            return Optional.empty();
        }
    }

    /** Put entry in cache. */
    public void putEntry(String tenantId, MemoryEntity entity) {
        try {
            String json = mapper.writeValueAsString(entity);
            redis.opsForValue().set(entryKey(tenantId, entity.getKey()), json, ENTRY_TTL);
        } catch (Exception e) {
            log.warn("Cache write failed for {}:{}", tenantId, entity.getKey(), e);
        }
    }

    /** Evict entry from cache (on update or delete). */
    public void evictEntry(String tenantId, String key) {
        try {
            redis.delete(entryKey(tenantId, key));
            redis.delete(contextKey(tenantId)); // context is now stale too
        } catch (Exception e) {
            log.warn("Cache evict failed for {}:{}", tenantId, key, e);
        }
    }

    // ── Context Cache ────────────────────────────────────────────

    /** Get cached context overview JSON, or empty. */
    public Optional<String> getContext(String tenantId) {
        try {
            return Optional.ofNullable(redis.opsForValue().get(contextKey(tenantId)));
        } catch (Exception e) {
            return Optional.empty();
        }
    }

    /** Cache context overview JSON. */
    public void putContext(String tenantId, String contextJson) {
        try {
            redis.opsForValue().set(contextKey(tenantId), contextJson, CONTEXT_TTL);
        } catch (Exception e) {
            log.warn("Context cache write failed for {}", tenantId, e);
        }
    }
}
