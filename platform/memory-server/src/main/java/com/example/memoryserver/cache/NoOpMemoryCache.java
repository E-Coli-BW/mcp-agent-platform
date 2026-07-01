package com.example.memoryserver.cache;

import com.example.memoryserver.model.MemoryEntity;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.stereotype.Component;

import java.util.Optional;

/**
 * No-op cache — used when Redis is not available.
 * Null Object Pattern: eliminates null checks throughout the codebase.
 * All methods are safe no-ops that return empty/false.
 */
@Component
@ConditionalOnMissingBean(MemoryCacheService.class)
public class NoOpMemoryCache implements MemoryCache {

    @Override
    public Optional<MemoryEntity> getEntry(String tenantId, String key) {
        return Optional.empty();
    }

    @Override
    public void putEntry(String tenantId, MemoryEntity entity) {
        // no-op
    }

    @Override
    public void evictEntry(String tenantId, String key) {
        // no-op
    }

    @Override
    public Optional<String> getContext(String tenantId) {
        return Optional.empty();
    }

    @Override
    public void putContext(String tenantId, String contextJson) {
        // no-op
    }
}
