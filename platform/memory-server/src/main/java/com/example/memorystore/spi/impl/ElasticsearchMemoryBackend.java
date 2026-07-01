package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;

import java.util.*;

/**
 * Elasticsearch-backed memory storage.
 *
 * Data model:
 *   Index: memory-{tenantId}
 *   Document ID: key
 *   Document body: { "key": "...", "content": "...", "tenant": "..." }
 *
 * Search uses ES full-text query on the "content" field.
 * For production, use the real ElasticsearchClient; for tests, use the stub.
 */
public class ElasticsearchMemoryBackend implements MemoryStorageBackend {

    private final EsOperations ops;

    /**
     * Abstraction over Elasticsearch client — allows real ES or a test stub.
     */
    public interface EsOperations {
        void index(String indexName, String id, String jsonBody);
        String get(String indexName, String id);
        boolean delete(String indexName, String id);
        List<String> listIds(String indexName);
        List<String> search(String indexName, String query);
    }

    public ElasticsearchMemoryBackend(EsOperations ops) {
        this.ops = ops;
    }

    private String indexName(String tenant) {
        return "memory-" + tenant;
    }

    @Override
    public void save(String tenant, String key, String value) {
        ops.index(indexName(tenant), key, value);
    }

    @Override
    public String load(String tenant, String key) {
        return ops.get(indexName(tenant), key);
    }

    @Override
    public boolean delete(String tenant, String key) {
        return ops.delete(indexName(tenant), key);
    }

    @Override
    public List<String> list(String tenant) {
        return ops.listIds(indexName(tenant));
    }

    @Override
    public List<String> search(String tenant, String query) {
        return ops.search(indexName(tenant), query);
    }
}
