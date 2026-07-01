package com.example.memorystore;

import com.example.memorystore.spi.impl.ElasticsearchMemoryBackend;
import com.example.memorystore.spi.impl.FileMemoryBackend;
import com.example.memorystore.spi.impl.NacosServiceDiscovery;
import com.example.memorystore.spi.impl.RedisMemoryBackend;
import com.example.memorystore.spi.impl.StaticServiceDiscovery;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.Nested;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

import static org.junit.jupiter.api.Assertions.*;

class MemoryStoreServiceTest {

    // ── Helper: in-memory Redis stub ──────────────────────────
    static class InMemoryRedisOps implements RedisMemoryBackend.RedisOperations {
        private final Map<String, Map<String, String>> store = new ConcurrentHashMap<>();

        @Override public void hset(String h, String f, String v) {
            store.computeIfAbsent(h, k -> new ConcurrentHashMap<>()).put(f, v);
        }
        @Override public String hget(String h, String f) {
            var m = store.get(h); return m == null ? null : m.get(f);
        }
        @Override public boolean hdel(String h, String f) {
            var m = store.get(h); return m != null && m.remove(f) != null;
        }
        @Override public Map<String, String> hgetAll(String h) {
            var m = store.get(h); return m == null ? Map.of() : new HashMap<>(m);
        }
        @Override public Set<String> hkeys(String h) {
            var m = store.get(h); return m == null ? Set.of() : m.keySet();
        }
    }

    // ── Helper: in-memory ES stub ──────────────────────────────
    static class InMemoryEsOps implements ElasticsearchMemoryBackend.EsOperations {
        private final Map<String, Map<String, String>> store = new ConcurrentHashMap<>();

        @Override public void index(String idx, String id, String body) {
            store.computeIfAbsent(idx, k -> new ConcurrentHashMap<>()).put(id, body);
        }
        @Override public String get(String idx, String id) {
            var m = store.get(idx); return m == null ? null : m.get(id);
        }
        @Override public boolean delete(String idx, String id) {
            var m = store.get(idx); return m != null && m.remove(id) != null;
        }
        @Override public List<String> listIds(String idx) {
            var m = store.get(idx); return m == null ? List.of() : new ArrayList<>(m.keySet());
        }
        @Override public List<String> search(String idx, String query) {
            var m = store.get(idx); if (m == null) return List.of();
            String lq = query.toLowerCase();
            return m.values().stream().filter(v -> v.toLowerCase().contains(lq))
                    .collect(java.util.stream.Collectors.toList());
        }
    }

    // ── File Backend Tests ────────────────────────────────────
    @Nested class FileBackendTests {
        private Path tempDir;
        private MemoryStoreService service;

        @BeforeEach void setUp() throws Exception {
            tempDir = Files.createTempDirectory("memstore-test");
            service = new MemoryStoreService(new FileMemoryBackend(tempDir),
                    new StaticServiceDiscovery(List.of("node1", "node2")));
        }
        @AfterEach void tearDown() throws Exception {
            Files.walk(tempDir).sorted((a, b) -> b.compareTo(a)).forEach(p -> p.toFile().delete());
        }

        @Test void saveAndLoad() {
            service.save("t1", "k1", "v1");
            assertEquals("v1", service.load("t1", "k1"));
        }
        @Test void loadMissing_returnsNull() {
            assertNull(service.load("t1", "no-such-key"));
        }
        @Test void delete() {
            service.save("t1", "k1", "v1");
            assertTrue(service.delete("t1", "k1"));
            assertNull(service.load("t1", "k1"));
        }
        @Test void list() {
            service.save("t1", "a", "1");
            service.save("t1", "b", "2");
            var keys = service.list("t1");
            assertEquals(2, keys.size());
            assertTrue(keys.containsAll(List.of("a", "b")));
        }
        @Test void search() {
            service.save("t1", "f1", "hello world");
            service.save("t1", "f2", "goodbye world");
            service.save("t1", "f3", "hello mars");
            assertEquals(2, service.search("t1", "world").size());
        }
        @Test void tenantIsolation() {
            service.save("t1", "k", "v1");
            service.save("t2", "k", "v2");
            assertEquals("v1", service.load("t1", "k"));
            assertEquals("v2", service.load("t2", "k"));
        }
        @Test void discovery() {
            assertEquals(List.of("node1", "node2"), service.getAvailableNodes());
        }
    }

    // ── Redis Backend Tests ───────────────────────────────────
    @Nested class RedisBackendTests {
        private MemoryStoreService service;

        @BeforeEach void setUp() {
            service = new MemoryStoreService(new RedisMemoryBackend(new InMemoryRedisOps()),
                    new StaticServiceDiscovery(List.of("redis-node1")));
        }

        @Test void saveAndLoad() {
            service.save("t1", "k1", "{\"content\":\"hello\"}");
            assertEquals("{\"content\":\"hello\"}", service.load("t1", "k1"));
        }
        @Test void loadMissing_returnsNull() {
            assertNull(service.load("t1", "no-such"));
        }
        @Test void delete() {
            service.save("t1", "k1", "v");
            assertTrue(service.delete("t1", "k1"));
            assertNull(service.load("t1", "k1"));
            assertFalse(service.delete("t1", "k1")); // double delete
        }
        @Test void list() {
            service.save("t1", "a", "1");
            service.save("t1", "b", "2");
            var keys = service.list("t1");
            assertEquals(2, keys.size());
            assertTrue(keys.containsAll(List.of("a", "b")));
        }
        @Test void search_caseInsensitive() {
            service.save("t1", "k1", "Hello World");
            service.save("t1", "k2", "goodbye");
            var results = service.search("t1", "hello");
            assertEquals(1, results.size());
            assertTrue(results.get(0).contains("Hello World"));
        }
        @Test void tenantIsolation() {
            service.save("t1", "k", "v1");
            service.save("t2", "k", "v2");
            assertEquals("v1", service.load("t1", "k"));
            assertEquals("v2", service.load("t2", "k"));
            assertEquals(1, service.list("t1").size());
        }
    }

    // ── Nacos Discovery Tests ─────────────────────────────────
    @Nested class NacosDiscoveryTests {
        @Test void returnsInstances() {
            var nacos = new NacosServiceDiscovery(
                    svc -> List.of("10.0.0.1:8080", "10.0.0.2:8080"));
            var service = new MemoryStoreService(
                    new RedisMemoryBackend(new InMemoryRedisOps()), nacos);
            assertEquals(List.of("10.0.0.1:8080", "10.0.0.2:8080"),
                    service.getAvailableNodes());
        }
        @Test void emptyCluster() {
            var nacos = new NacosServiceDiscovery(svc -> List.of());
            var service = new MemoryStoreService(
                    new RedisMemoryBackend(new InMemoryRedisOps()), nacos);
            assertTrue(service.getAvailableNodes().isEmpty());
        }
    }

    // ── Elasticsearch Backend Tests ─────────────────────────────
    @Nested class ElasticsearchBackendTests {
        private MemoryStoreService service;

        @BeforeEach void setUp() {
            service = new MemoryStoreService(new ElasticsearchMemoryBackend(new InMemoryEsOps()),
                    new StaticServiceDiscovery(List.of("es-node1")));
        }

        @Test void saveAndLoad() {
            service.save("t1", "k1", "{\"content\":\"hello ES\"}");
            assertEquals("{\"content\":\"hello ES\"}", service.load("t1", "k1"));
        }
        @Test void loadMissing_returnsNull() {
            assertNull(service.load("t1", "no-such"));
        }
        @Test void delete() {
            service.save("t1", "k1", "v");
            assertTrue(service.delete("t1", "k1"));
            assertNull(service.load("t1", "k1"));
            assertFalse(service.delete("t1", "k1"));
        }
        @Test void list() {
            service.save("t1", "a", "1");
            service.save("t1", "b", "2");
            var keys = service.list("t1");
            assertEquals(2, keys.size());
            assertTrue(keys.containsAll(List.of("a", "b")));
        }
        @Test void search_caseInsensitive() {
            service.save("t1", "k1", "Hello World");
            service.save("t1", "k2", "goodbye");
            var results = service.search("t1", "hello");
            assertEquals(1, results.size());
        }
        @Test void tenantIsolation() {
            service.save("t1", "k", "v1");
            service.save("t2", "k", "v2");
            assertEquals("v1", service.load("t1", "k"));
            assertEquals("v2", service.load("t2", "k"));
        }
    }

    // ── Backend Swappability Test ─────────────────────────────
    @Nested class BackendSwapTests {
        @Test void sameServiceContract_allThreeBackends() throws Exception {
            // File backend
            Path tmp = Files.createTempDirectory("swap-test");
            var fileSvc = new MemoryStoreService(new FileMemoryBackend(tmp),
                    new StaticServiceDiscovery(List.of()));
            fileSvc.save("t", "k", "data");
            assertEquals("data", fileSvc.load("t", "k"));

            // Redis backend — same API, same result
            var redisSvc = new MemoryStoreService(new RedisMemoryBackend(new InMemoryRedisOps()),
                    new StaticServiceDiscovery(List.of()));
            redisSvc.save("t", "k", "data");
            assertEquals("data", redisSvc.load("t", "k"));

            // ES backend — same API, same result
            var esSvc = new MemoryStoreService(new ElasticsearchMemoryBackend(new InMemoryEsOps()),
                    new StaticServiceDiscovery(List.of()));
            esSvc.save("t", "k", "data");
            assertEquals("data", esSvc.load("t", "k"));

            // Cleanup
            Files.walk(tmp).sorted((a, b) -> b.compareTo(a)).forEach(p -> p.toFile().delete());
        }
    }
}
