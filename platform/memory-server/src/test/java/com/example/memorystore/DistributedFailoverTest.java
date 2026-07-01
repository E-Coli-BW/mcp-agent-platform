package com.example.memorystore;

import com.example.memorystore.spi.MemoryStorageBackend;
import com.example.memorystore.spi.impl.CircuitBreakerBackend;
import com.example.memorystore.spi.impl.CircuitBreakerBackend.CircuitBreakerOpenException;
import com.example.memorystore.spi.impl.RedisMemoryBackend;
import com.example.memorystore.spi.impl.StaticServiceDiscovery;
import org.junit.jupiter.api.*;

import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Phase 3 tests: failover, circuit breaker, concurrent scaling.
 */
class DistributedFailoverTest {

    // ── Flaky backend that fails N times then recovers ────────
    static class FlakyBackend implements MemoryStorageBackend {
        private final MemoryStorageBackend delegate;
        private final AtomicInteger callCount = new AtomicInteger(0);
        private volatile int failUntilCall; // fail for the first N calls

        FlakyBackend(MemoryStorageBackend delegate, int failUntilCall) {
            this.delegate = delegate;
            this.failUntilCall = failUntilCall;
        }

        void setFailUntilCall(int n) { this.failUntilCall = n; }

        private void maybeThrow() {
            if (callCount.incrementAndGet() <= failUntilCall) {
                throw new RuntimeException("Simulated backend failure #" + callCount.get());
            }
        }

        @Override public void save(String t, String k, String v) { maybeThrow(); delegate.save(t, k, v); }
        @Override public String load(String t, String k) { maybeThrow(); return delegate.load(t, k); }
        @Override public boolean delete(String t, String k) { maybeThrow(); return delegate.delete(t, k); }
        @Override public List<String> list(String t) { maybeThrow(); return delegate.list(t); }
        @Override public List<String> search(String t, String q) { maybeThrow(); return delegate.search(t, q); }
    }

    // ── In-memory Redis stub (reuse from MemoryStoreServiceTest) ──
    static class InMemoryRedisOps implements RedisMemoryBackend.RedisOperations {
        private final Map<String, Map<String, String>> store = new ConcurrentHashMap<>();
        @Override public void hset(String h, String f, String v) { store.computeIfAbsent(h, k -> new ConcurrentHashMap<>()).put(f, v); }
        @Override public String hget(String h, String f) { var m = store.get(h); return m == null ? null : m.get(f); }
        @Override public boolean hdel(String h, String f) { var m = store.get(h); return m != null && m.remove(f) != null; }
        @Override public Map<String, String> hgetAll(String h) { var m = store.get(h); return m == null ? Map.of() : new HashMap<>(m); }
        @Override public Set<String> hkeys(String h) { var m = store.get(h); return m == null ? Set.of() : m.keySet(); }
    }

    // ── Circuit Breaker Tests ─────────────────────────────────
    @Nested class CircuitBreakerTests {

        @Test void closedState_normalOperation() {
            var redis = new RedisMemoryBackend(new InMemoryRedisOps());
            var cb = new CircuitBreakerBackend(redis, 3, 1000);
            assertEquals(CircuitBreakerBackend.State.CLOSED, cb.getState());

            cb.save("t1", "k1", "v1");
            assertEquals("v1", cb.load("t1", "k1"));
            assertEquals(CircuitBreakerBackend.State.CLOSED, cb.getState());
        }

        @Test void opensAfterThresholdFailures() {
            var flaky = new FlakyBackend(new RedisMemoryBackend(new InMemoryRedisOps()), 100);
            var cb = new CircuitBreakerBackend(flaky, 3, 5000);

            // 3 failures should open the circuit
            for (int i = 0; i < 3; i++) {
                assertThrows(RuntimeException.class, () -> cb.save("t", "k", "v"));
            }
            assertEquals(CircuitBreakerBackend.State.OPEN, cb.getState());

            // Next call should fail fast with CircuitBreakerOpenException
            assertThrows(CircuitBreakerOpenException.class, () -> cb.save("t", "k", "v"));
        }

        @Test void recoversAfterCooldown() throws InterruptedException {
            var flaky = new FlakyBackend(new RedisMemoryBackend(new InMemoryRedisOps()), 3);
            var cb = new CircuitBreakerBackend(flaky, 3, 100); // 100ms cooldown

            // Trigger open
            for (int i = 0; i < 3; i++) {
                assertThrows(RuntimeException.class, () -> cb.save("t", "k", "v"));
            }
            assertEquals(CircuitBreakerBackend.State.OPEN, cb.getState());

            // Wait for cooldown
            Thread.sleep(150);

            // Backend has recovered (flaky only fails first 3 calls)
            cb.save("t", "k", "recovered");
            assertEquals(CircuitBreakerBackend.State.CLOSED, cb.getState());
            assertEquals("recovered", cb.load("t", "k"));
        }

        @Test void halfOpenFailsReturnsToOpen() throws InterruptedException {
            var flaky = new FlakyBackend(new RedisMemoryBackend(new InMemoryRedisOps()), 100);
            var cb = new CircuitBreakerBackend(flaky, 3, 100);

            // Trigger open
            for (int i = 0; i < 3; i++) {
                assertThrows(RuntimeException.class, () -> cb.save("t", "k", "v"));
            }

            // Wait for cooldown → HALF_OPEN → probe fails → back to OPEN
            Thread.sleep(150);
            assertThrows(RuntimeException.class, () -> cb.save("t", "k", "v"));
            assertEquals(CircuitBreakerBackend.State.OPEN, cb.getState());
        }
    }

    // ── Concurrent Scaling Tests ──────────────────────────────
    @Nested class ConcurrentScalingTests {

        @Test void concurrentWritesAreThreadSafe() throws Exception {
            var redis = new RedisMemoryBackend(new InMemoryRedisOps());
            var service = new MemoryStoreService(redis,
                    new StaticServiceDiscovery(List.of("node1")));

            int threads = 10;
            int opsPerThread = 100;
            var executor = Executors.newFixedThreadPool(threads);
            var latch = new CountDownLatch(threads);

            for (int t = 0; t < threads; t++) {
                final int threadId = t;
                executor.submit(() -> {
                    try {
                        for (int i = 0; i < opsPerThread; i++) {
                            String key = "t" + threadId + "-k" + i;
                            service.save("tenant", key, "value-" + threadId + "-" + i);
                        }
                    } finally {
                        latch.countDown();
                    }
                });
            }
            assertTrue(latch.await(10, TimeUnit.SECONDS));
            executor.shutdown();

            // Verify all writes
            var keys = service.list("tenant");
            assertEquals(threads * opsPerThread, keys.size());
        }

        @Test void multiTenantConcurrentIsolation() throws Exception {
            var redis = new RedisMemoryBackend(new InMemoryRedisOps());
            var service = new MemoryStoreService(redis,
                    new StaticServiceDiscovery(List.of()));

            int tenants = 5;
            int keysPerTenant = 50;
            var executor = Executors.newFixedThreadPool(tenants);
            var latch = new CountDownLatch(tenants);

            for (int t = 0; t < tenants; t++) {
                final String tenant = "tenant-" + t;
                executor.submit(() -> {
                    try {
                        for (int i = 0; i < keysPerTenant; i++) {
                            service.save(tenant, "k" + i, tenant + "-val-" + i);
                        }
                    } finally {
                        latch.countDown();
                    }
                });
            }
            assertTrue(latch.await(10, TimeUnit.SECONDS));
            executor.shutdown();

            // Each tenant should see exactly their own keys
            for (int t = 0; t < tenants; t++) {
                String tenant = "tenant-" + t;
                assertEquals(keysPerTenant, service.list(tenant).size());
                assertEquals(tenant + "-val-0", service.load(tenant, "k0"));
            }
        }
    }

    // ── Multi-Node Discovery Failover ─────────────────────────
    @Nested class DiscoveryFailoverTests {

        @Test void discoveryReturnsLiveNodes() {
            // Simulate: 3 nodes registered, 1 goes down
            List<String> allNodes = new ArrayList<>(List.of("node1:8080", "node2:8080", "node3:8080"));
            var discovery = new StaticServiceDiscovery(allNodes);
            var service = new MemoryStoreService(
                    new RedisMemoryBackend(new InMemoryRedisOps()), discovery);

            assertEquals(3, service.getAvailableNodes().size());

            // Simulate node removal (in real Nacos, health check would remove it)
            allNodes.remove("node2:8080");
            assertEquals(2, service.getAvailableNodes().size());
            assertFalse(service.getAvailableNodes().contains("node2:8080"));
        }

        @Test void circuitBreakerWithDiscovery() {
            // Circuit breaker protects against backend failure, discovery is independent
            var flaky = new FlakyBackend(new RedisMemoryBackend(new InMemoryRedisOps()), 3);
            var cb = new CircuitBreakerBackend(flaky, 3, 5000);
            var service = new MemoryStoreService(cb,
                    new StaticServiceDiscovery(List.of("node1", "node2")));

            // Discovery still works even when backend circuit is open
            for (int i = 0; i < 3; i++) {
                assertThrows(RuntimeException.class, () -> service.save("t", "k", "v"));
            }
            // Backend is open, but discovery is fine
            assertEquals(List.of("node1", "node2"), service.getAvailableNodes());
            assertThrows(CircuitBreakerOpenException.class, () -> service.load("t", "k"));
        }
    }
}
