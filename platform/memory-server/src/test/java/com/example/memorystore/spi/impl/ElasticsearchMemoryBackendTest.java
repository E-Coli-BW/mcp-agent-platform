package com.example.memorystore.spi.impl;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for ElasticsearchMemoryBackend using an in-memory stub.
 * Tests the backend logic without needing a running ES instance.
 */
class ElasticsearchMemoryBackendTest {

    private InMemoryEsOps stubOps;
    private ElasticsearchMemoryBackend backend;

    @BeforeEach
    void setup() {
        stubOps = new InMemoryEsOps();
        backend = new ElasticsearchMemoryBackend(stubOps);
    }

    @Test
    void should_saveAndLoad_when_validKeyAndValue() {
        backend.save("tenant-1", "greeting", "hello world");

        String result = backend.load("tenant-1", "greeting");
        assertEquals("hello world", result);
    }

    @Test
    void should_returnNull_when_keyNotFound() {
        String result = backend.load("tenant-1", "nonexistent");
        assertNull(result);
    }

    @Test
    void should_isolateTenants_when_sameKeyDifferentTenant() {
        backend.save("tenant-a", "key1", "value-a");
        backend.save("tenant-b", "key1", "value-b");

        assertEquals("value-a", backend.load("tenant-a", "key1"));
        assertEquals("value-b", backend.load("tenant-b", "key1"));
    }

    @Test
    void should_deleteEntry_when_keyExists() {
        backend.save("tenant-1", "to-delete", "temp");

        boolean deleted = backend.delete("tenant-1", "to-delete");
        assertTrue(deleted);
        assertNull(backend.load("tenant-1", "to-delete"));
    }

    @Test
    void should_returnFalse_when_deletingNonexistentKey() {
        boolean deleted = backend.delete("tenant-1", "ghost");
        assertFalse(deleted);
    }

    @Test
    void should_listAllKeys_when_multipleEntriesExist() {
        backend.save("tenant-1", "key-a", "val-a");
        backend.save("tenant-1", "key-b", "val-b");
        backend.save("tenant-1", "key-c", "val-c");

        List<String> keys = backend.list("tenant-1");
        assertEquals(3, keys.size());
        assertTrue(keys.contains("key-a"));
        assertTrue(keys.contains("key-b"));
        assertTrue(keys.contains("key-c"));
    }

    @Test
    void should_returnEmptyList_when_noEntriesForTenant() {
        List<String> keys = backend.list("empty-tenant");
        assertTrue(keys.isEmpty());
    }

    @Test
    void should_searchByContent_when_queryMatches() {
        backend.save("tenant-1", "note1", "spring boot microservice");
        backend.save("tenant-1", "note2", "kafka event streaming");
        backend.save("tenant-1", "note3", "spring cloud gateway");

        List<String> results = backend.search("tenant-1", "spring");
        assertEquals(2, results.size());
    }

    @Test
    void should_returnEmptySearch_when_noMatch() {
        backend.save("tenant-1", "note1", "java backend");

        List<String> results = backend.search("tenant-1", "python");
        assertTrue(results.isEmpty());
    }

    @Test
    void should_overwriteValue_when_savingExistingKey() {
        backend.save("tenant-1", "mutable", "version-1");
        backend.save("tenant-1", "mutable", "version-2");

        assertEquals("version-2", backend.load("tenant-1", "mutable"));
    }

    @Test
    void should_useCorrectIndexName_when_tenantHasSpecialChars() {
        // Index name should be memory-{tenantId}
        backend.save("my-org", "key1", "value1");
        // Verify it's stored under "memory-my-org" index
        assertNotNull(stubOps.getStore().get("memory-my-org"));
    }

    // ─── In-Memory Stub ────────────────────────────────────────────────

    /**
     * In-memory implementation of EsOperations for unit testing.
     * Simulates index-per-tenant with simple substring search.
     */
    static class InMemoryEsOps implements ElasticsearchMemoryBackend.EsOperations {
        private final Map<String, Map<String, String>> store = new HashMap<>();

        @Override
        public void index(String indexName, String id, String jsonBody) {
            store.computeIfAbsent(indexName, k -> new HashMap<>()).put(id, jsonBody);
        }

        @Override
        public String get(String indexName, String id) {
            Map<String, String> index = store.get(indexName);
            return index != null ? index.get(id) : null;
        }

        @Override
        public boolean delete(String indexName, String id) {
            Map<String, String> index = store.get(indexName);
            if (index != null && index.containsKey(id)) {
                index.remove(id);
                return true;
            }
            return false;
        }

        @Override
        public List<String> listIds(String indexName) {
            Map<String, String> index = store.get(indexName);
            return index != null ? new ArrayList<>(index.keySet()) : List.of();
        }

        @Override
        public List<String> search(String indexName, String query) {
            Map<String, String> index = store.get(indexName);
            if (index == null) return List.of();
            List<String> results = new ArrayList<>();
            for (Map.Entry<String, String> entry : index.entrySet()) {
                if (entry.getValue().toLowerCase().contains(query.toLowerCase())) {
                    results.add(entry.getValue());
                }
            }
            return results;
        }

        Map<String, Map<String, String>> getStore() { return store; }
    }
}
