package com.example.memoryserver.cache;

import com.example.memoryserver.model.MemoryEntity;

import java.util.Optional;

/**
 * Abstraction for memory cache operations.
 * Implementations: MemoryCacheService (Redis) and NoOpMemoryCache (fallback).
 */
public interface MemoryCache {

    Optional<MemoryEntity> getEntry(String tenantId, String key);

    void putEntry(String tenantId, MemoryEntity entity);

    void evictEntry(String tenantId, String key);

    Optional<String> getContext(String tenantId);

    void putContext(String tenantId, String contextJson);
}
