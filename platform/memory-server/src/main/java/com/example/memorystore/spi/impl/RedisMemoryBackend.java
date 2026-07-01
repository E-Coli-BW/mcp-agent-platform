package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;

import java.util.*;
import java.util.stream.Collectors;

/**
 * Redis-backed memory storage using hash-per-tenant pattern.
 *
 * Data model:
 *   HASH  memory:{tenantId}  →  { key1: value1, key2: value2, ... }
 *
 * Search uses server-side HSCAN + client-side content matching.
 * For production at scale, consider Redis Search (RediSearch) module.
 */
public class RedisMemoryBackend implements MemoryStorageBackend {

    private final RedisOperations ops;

    /**
     * Abstraction over Redis commands — allows real Jedis/Lettuce or a test stub.
     */
    public interface RedisOperations {
        void hset(String hashKey, String field, String value);
        String hget(String hashKey, String field);
        boolean hdel(String hashKey, String field);
        Map<String, String> hgetAll(String hashKey);
        Set<String> hkeys(String hashKey);
    }

    public RedisMemoryBackend(RedisOperations ops) {
        this.ops = ops;
    }

    private String hashKey(String tenant) {
        return "memory:" + tenant;
    }

    @Override
    public void save(String tenant, String key, String value) {
        ops.hset(hashKey(tenant), key, value);
    }

    @Override
    public String load(String tenant, String key) {
        return ops.hget(hashKey(tenant), key);
    }

    @Override
    public boolean delete(String tenant, String key) {
        return ops.hdel(hashKey(tenant), key);
    }

    @Override
    public List<String> list(String tenant) {
        Set<String> keys = ops.hkeys(hashKey(tenant));
        return keys == null ? List.of() : new ArrayList<>(keys);
    }

    @Override
    public List<String> search(String tenant, String query) {
        Map<String, String> all = ops.hgetAll(hashKey(tenant));
        if (all == null || all.isEmpty()) return List.of();
        String lowerQuery = query.toLowerCase();
        return all.values().stream()
                .filter(v -> v.toLowerCase().contains(lowerQuery))
                .collect(Collectors.toList());
    }
}
