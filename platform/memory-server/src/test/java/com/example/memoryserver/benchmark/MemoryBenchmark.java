package com.example.memoryserver.benchmark;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.ActiveProfiles;

import javax.crypto.SecretKey;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Production-like QPS benchmark for Memory Server.
 *
 * Requires: Docker infrastructure (PostgreSQL + Redis)
 *   make docker-up
 *   Then run with: -Dspring.profiles.active=docker-test
 *
 * NOT included in normal test runs — must be invoked explicitly:
 *   mvn test -Dtest=MemoryBenchmark -Dspring.profiles.active=docker-test
 *
 * See BENCHMARK-TEST-PLAN.md for the full credibility argument.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("integration-test")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
@Tag("benchmark")  // excluded from normal test runs via surefire config
class MemoryBenchmark {

    @LocalServerPort
    private int port;

    @Value("${mcp.security.jwt-secret:default-dev-secret-change-in-production}")
    private String jwtSecret;

    private final HttpClient httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();
    private final ObjectMapper mapper = new ObjectMapper();
    private final ThreadLocal<Random> threadRandom = ThreadLocal.withInitial(() -> new Random());
    private final Random seedRandom = new Random(42); // for single-threaded seeding

    // ── Config ────────────────────────────────────────────────
    private static final int NUM_TENANTS = 20;
    private static final int ENTRIES_PER_TENANT = 500;
    private static final int WARM_UP_SECONDS = 3;

    private static final String[] NAMESPACES = {"default", "skills", "project", "debug", "preferences"};
    private static final String[] TAGS = {"java", "python", "docker", "security", "performance",
            "spring", "redis", "kafka", "agent", "memory"};
    private static final String[] SEARCH_QUERIES = {"spring security", "docker container",
            "maven build", "redis cache", "kafka event", "memory search", "jwt token",
            "circuit breaker", "tenant isolation", "optimistic lock"};

    // ── Helpers ───────────────────────────────────────────────

    private String baseUrl() {
        return "http://localhost:" + port;
    }

    private String generateJwt(String tenantId) {
        byte[] keyBytes = jwtSecret.getBytes(StandardCharsets.UTF_8);
        if (keyBytes.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
            keyBytes = padded;
        }
        SecretKey key = Keys.hmacShaKeyFor(keyBytes);
        // FIX: Use ROLE_ prefix for Spring Security
        return Jwts.builder()
                .subject("bench-client")
                .claim("tenant_id", tenantId)
                .claim("roles", List.of("ROLE_SERVICE", "ROLE_MEMORY_READ", "ROLE_MEMORY_WRITE"))
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plus(1, ChronoUnit.HOURS)))
                .signWith(key)
                .compact();
    }

    private HttpResponse<String> post(String path, String body, String jwt) throws Exception {
        var request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + path))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + jwt)
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .timeout(Duration.ofSeconds(10))
                .build();
        return httpClient.send(request, HttpResponse.BodyHandlers.ofString());
    }

    private String randomContent(int length) {
        var sb = new StringBuilder(length);
        String words = "the quick brown fox jumps over lazy dog spring boot java memory redis kafka docker security tenant ";
        while (sb.length() < length) {
            sb.append(words);
        }
        return sb.substring(0, length);
    }

    // ── Data Seeding ──────────────────────────────────────────

    @Test
    @Order(1)
    @DisplayName("S0: Seed 10K entries across 20 tenants")
    void seedData() throws Exception {
        System.out.println("\n═══ SEEDING DATA ═══");
        long start = System.currentTimeMillis();
        AtomicInteger count = new AtomicInteger(0);

        for (int t = 0; t < NUM_TENANTS; t++) {
            String tenant = "bench-tenant-" + t;
            String jwt = generateJwt(tenant);
            for (int i = 0; i < ENTRIES_PER_TENANT; i++) {
                String body = mapper.writeValueAsString(Map.of(
                        "key", "entry-" + i,
                        "content", randomContent(200 + seedRandom.nextInt(1800)),
                        "namespace", NAMESPACES[i % 5],
                        "tags", List.of(TAGS[i % 10], TAGS[(i + 3) % 10]),
                        "pinned", i % 10 == 0
                ));
                var resp = post("/api/tools/memory_set", body, jwt);
                if (resp.statusCode() == 200) count.incrementAndGet();
            }
            System.out.printf("  Tenant %d/%d seeded (%d entries)%n", t + 1, NUM_TENANTS, count.get());
        }

        long elapsed = System.currentTimeMillis() - start;
        System.out.printf("  ✅ Seeded %d entries in %.1fs (%.0f entries/sec)%n",
                count.get(), elapsed / 1000.0, count.get() * 1000.0 / elapsed);
        assertEquals(NUM_TENANTS * ENTRIES_PER_TENANT, count.get());
    }

    // ── Scenario 1: Context Read ──────────────────────────────

    @Test
    @Order(2)
    @DisplayName("S1: Context Read — 50 concurrent, 30s")
    void scenario1_contextRead() throws Exception {
        BenchmarkResult result = runBenchmark("S1: Context Read", 50, 30, (threadId) -> {
            String tenant = "bench-tenant-" + (threadId % NUM_TENANTS);
            String jwt = generateJwt(tenant);
            return post("/api/tools/memory_context", "{}", jwt);
        });

        recordResult(result);
        assertTrue(result.errorRate() < 0.01, "Error rate should be < 1%");
        assertTrue(result.p99Ms < 500, "P99 should be < 500ms, got " + result.p99Ms);
    }

    // ── Scenario 2: Write Contention ──────────────────────────

    @Test
    @Order(3)
    @DisplayName("S2: Write Contention — 50 concurrent, 30s, 10% hot keys")
    void scenario2_writeContention() throws Exception {
        BenchmarkResult result = runBenchmark("S2: Write Contention", 50, 30, (threadId) -> {
            String tenant = "bench-tenant-" + (threadId % NUM_TENANTS);
            String jwt = generateJwt(tenant);

            // 10% hot keys (shared across threads), 90% cold keys (unique)
            String key;
            if (threadRandom.get().nextInt(10) == 0) {
                key = "hot-key-" + (threadRandom.get().nextInt(50)); // 50 hot keys
            } else {
                key = "cold-" + threadId + "-" + System.nanoTime();
            }

            String body = mapper.writeValueAsString(Map.of(
                    "key", key,
                    "content", randomContent(200 + threadRandom.get().nextInt(1800))
            ));
            return post("/api/tools/memory_set", body, jwt);
        });

        recordResult(result);
        assertTrue(result.errorRate() < 0.02, "Error rate should be < 2%, got " + result.errorRate());
        // P99 includes tail latency from optimistic lock retries on 10% hot-key contention
        assertTrue(result.p99Ms < 1000, "P99 should be < 1s, got " + result.p99Ms);
    }

    // ── Scenario 3: Mixed Workload ────────────────────────────

    @Test
    @Order(4)
    @DisplayName("S3: Mixed — 50 concurrent, 60s, 70/20/10 search/set/context")
    void scenario3_mixedWorkload() throws Exception {
        BenchmarkResult result = runBenchmark("S3: Mixed Workload", 50, 60, (threadId) -> {
            String tenant = "bench-tenant-" + (threadId % NUM_TENANTS);
            String jwt = generateJwt(tenant);
            int roll = threadRandom.get().nextInt(100);

            if (roll < 70) {
                // 70% search
                String query = SEARCH_QUERIES[threadRandom.get().nextInt(SEARCH_QUERIES.length)];
                String body = mapper.writeValueAsString(Map.of("query", query));
                return post("/api/tools/memory_search", body, jwt);
            } else if (roll < 90) {
                // 20% set
                String body = mapper.writeValueAsString(Map.of(
                        "key", "mixed-" + threadId + "-" + System.nanoTime(),
                        "content", randomContent(300 + threadRandom.get().nextInt(1000))
                ));
                return post("/api/tools/memory_set", body, jwt);
            } else {
                // 10% context
                return post("/api/tools/memory_context", "{}", jwt);
            }
        });

        recordResult(result);
        assertTrue(result.errorRate() < 0.01, "Error rate should be < 1%");
        assertTrue(result.p99Ms < 1000, "P99 should be < 1s (mixed includes writes), got " + result.p99Ms);
    }

    // ── Validation ────────────────────────────────────────────

    @Test
    @Order(5)
    @DisplayName("S4: Tenant Isolation Validation")
    void scenario4_tenantIsolation() throws Exception {
        System.out.println("\n═══ TENANT ISOLATION CHECK ═══");

        // Use a fresh client to avoid stale connections from prior load tests
        var isolationClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();

        // Tenant 0 searches for content that only tenant 1 has
        String jwt0 = generateJwt("bench-tenant-0");
        String jwt1 = generateJwt("bench-tenant-1");

        // Set unique content in tenant 1
        String uniqueContent = "UNIQUE-ISOLATION-TEST-" + UUID.randomUUID();
        var setReq = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_set"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + jwt1)
                .POST(HttpRequest.BodyPublishers.ofString(
                        mapper.writeValueAsString(Map.of("key", "isolation-test", "content", uniqueContent))))
                .timeout(Duration.ofSeconds(30))
                .build();
        isolationClient.send(setReq, HttpResponse.BodyHandlers.ofString());

        // Search from tenant 0 — must NOT find tenant 1's data
        var searchReq0 = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_search"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + jwt0)
                .POST(HttpRequest.BodyPublishers.ofString(
                        mapper.writeValueAsString(Map.of("query", "UNIQUE-ISOLATION-TEST"))))
                .timeout(Duration.ofSeconds(30))
                .build();
        var resp = isolationClient.send(searchReq0, HttpResponse.BodyHandlers.ofString());

        assertFalse(resp.body().contains(uniqueContent),
                "SECURITY: Tenant 0 found tenant 1's data!");

        // Search from tenant 1 — should find it
        var searchReq1 = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_search"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + jwt1)
                .POST(HttpRequest.BodyPublishers.ofString(
                        mapper.writeValueAsString(Map.of("query", "UNIQUE-ISOLATION-TEST"))))
                .timeout(Duration.ofSeconds(30))
                .build();
        var resp1 = isolationClient.send(searchReq1, HttpResponse.BodyHandlers.ofString());
        assertTrue(resp1.body().contains("isolation-test"),
                "Tenant 1 should find its own data");

        System.out.println("  ✅ Tenant isolation verified — zero cross-tenant leaks");
    }

    // ── Benchmark Runner ──────────────────────────────────────

    @FunctionalInterface
    interface BenchmarkOperation {
        HttpResponse<String> execute(int threadId) throws Exception;
    }

    record BenchmarkResult(
            String name,
            int concurrency,
            int durationSec,
            long totalOps,
            long successOps,
            long errorOps,
            long p50Ms,
            long p95Ms,
            long p99Ms,
            double opsPerSec
    ) {
        double errorRate() {
            return totalOps == 0 ? 0 : (double) errorOps / totalOps;
        }

        void print() {
            System.out.printf("""
                    
                    ═══ %s ═══════════════════════════════════
                      Concurrency:  %d threads
                      Duration:     %ds
                      Throughput:   %.0f ops/sec
                      P50:          %dms
                      P95:          %dms
                      P99:          %dms
                      Total ops:    %d (success: %d, errors: %d)
                      Error rate:   %.2f%%
                    ════════════════════════════════════════════
                    """,
                    name, concurrency, durationSec, opsPerSec,
                    p50Ms, p95Ms, p99Ms,
                    totalOps, successOps, errorOps, errorRate() * 100);
        }
    }

    private BenchmarkResult runBenchmark(String name, int concurrency, int durationSec,
                                          BenchmarkOperation operation) throws Exception {
        System.out.printf("%n═══ %s (warm-up %ds, measure %ds, %d threads) ═══%n",
                name, WARM_UP_SECONDS, durationSec, concurrency);

        var latencies = new ConcurrentLinkedQueue<Long>();
        var successCount = new AtomicLong(0);
        var errorCount = new AtomicLong(0);
        var running = new AtomicInteger(1); // 1 = running, 0 = stop

        var executor = Executors.newVirtualThreadPerTaskExecutor();
        var startTime = System.currentTimeMillis();
        var warmUpEnd = startTime + WARM_UP_SECONDS * 1000L;
        var benchEnd = warmUpEnd + durationSec * 1000L;

        // Launch concurrent workers
        var futures = new ArrayList<Future<?>>();
        for (int t = 0; t < concurrency; t++) {
            final int threadId = t;
            futures.add(executor.submit(() -> {
                while (running.get() == 1 && System.currentTimeMillis() < benchEnd) {
                    long opStart = System.currentTimeMillis();
                    try {
                        var resp = operation.execute(threadId);
                        long elapsed = System.currentTimeMillis() - opStart;

                        // Only record after warm-up
                        if (System.currentTimeMillis() > warmUpEnd) {
                            if (resp.statusCode() == 200) {
                                successCount.incrementAndGet();
                            } else {
                                errorCount.incrementAndGet();
                            }
                            latencies.add(elapsed);
                        }
                        // Small backoff to simulate realistic client pacing
                        Thread.sleep(1);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        break;
                    } catch (Exception e) {
                        if (System.currentTimeMillis() > warmUpEnd) {
                            errorCount.incrementAndGet();
                            latencies.add(System.currentTimeMillis() - opStart);
                        }
                    }
                }
            }));
        }

        // Wait for completion
        Thread.sleep((WARM_UP_SECONDS + durationSec) * 1000L + 2000);
        running.set(0);
        executor.shutdown();
        executor.awaitTermination(10, TimeUnit.SECONDS);

        // Calculate percentiles
        var sorted = latencies.stream().sorted().collect(Collectors.toList());
        long total = successCount.get() + errorCount.get();

        long p50 = sorted.isEmpty() ? 0 : sorted.get((int) (sorted.size() * 0.50));
        long p95 = sorted.isEmpty() ? 0 : sorted.get((int) (sorted.size() * 0.95));
        long p99 = sorted.isEmpty() ? 0 : sorted.get(Math.min((int) (sorted.size() * 0.99), sorted.size() - 1));
        double opsPerSec = total * 1000.0 / (durationSec * 1000.0);

        return new BenchmarkResult(name, concurrency, durationSec,
                total, successCount.get(), errorCount.get(),
                p50, p95, p99, opsPerSec);
    }

    // ── Report Generation ────────────────────────────────────────

    private static final List<BenchmarkResult> allResults = new ArrayList<>();

    private void recordResult(BenchmarkResult result) {
        result.print();
        allResults.add(result);
    }

    @AfterAll
    static void generateReport() {
        if (allResults.isEmpty()) return;

        System.out.println("\n\n");
        System.out.println("╔══════════════════════════════════════════════════════════════════╗");
        System.out.println("║           MEMORY SERVER QPS BENCHMARK REPORT                    ║");
        System.out.println("╠══════════════════════════════════════════════════════════════════╣");
        System.out.printf( "║  Date:           %-47s║%n", Instant.now().toString());
        System.out.printf( "║  Infrastructure: %-47s║%n", "PostgreSQL 16 + Redis 7 (Docker)");
        System.out.printf( "║  JDK:            %-47s║%n", System.getProperty("java.version"));
        System.out.println("╠══════════════════════════════════════════════════════════════════╣");
        System.out.println("║  Scenario               │ ops/s │  P50  │  P95  │  P99  │ Err% ║");
        System.out.println("╠─────────────────────────┼───────┼───────┼───────┼───────┼──────╣");
        for (var r : allResults) {
            System.out.printf("║  %-23s│ %5.0f │ %3dms │ %3dms │ %3dms │%4.1f%% ║%n",
                    truncateName(r.name(), 23), r.opsPerSec(),
                    r.p50Ms(), r.p95Ms(), r.p99Ms(), r.errorRate() * 100);
        }
        System.out.println("╚══════════════════════════════════════════════════════════════════╝");

        // Save JSON report to target/
        try {
            var reportMap = new LinkedHashMap<String, Object>();
            reportMap.put("timestamp", Instant.now().toString());
            reportMap.put("jdk", System.getProperty("java.version"));
            reportMap.put("infrastructure", "PostgreSQL 16 + Redis 7");

            var scenarios = new ArrayList<Map<String, Object>>();
            for (var r : allResults) {
                var s = new LinkedHashMap<String, Object>();
                s.put("name", r.name());
                s.put("concurrency", r.concurrency());
                s.put("durationSec", r.durationSec());
                s.put("opsPerSec", Math.round(r.opsPerSec()));
                s.put("p50ms", r.p50Ms());
                s.put("p95ms", r.p95Ms());
                s.put("p99ms", r.p99Ms());
                s.put("totalOps", r.totalOps());
                s.put("successOps", r.successOps());
                s.put("errorOps", r.errorOps());
                s.put("errorRate", String.format("%.2f%%", r.errorRate() * 100));
                scenarios.add(s);
            }
            reportMap.put("scenarios", scenarios);

            // Use absolute path so the file is always findable regardless of CWD
            var projectDir = java.nio.file.Path.of(System.getProperty("user.dir"));
            var reportFile = projectDir.resolve("target").resolve("benchmark-results.json");
            java.nio.file.Files.createDirectories(reportFile.getParent());
            java.nio.file.Files.writeString(reportFile,
                    new ObjectMapper().writerWithDefaultPrettyPrinter().writeValueAsString(reportMap));
            System.out.println("\n📄 Report saved to: " + reportFile.toAbsolutePath());
        } catch (Exception e) {
            System.err.println("Failed to save benchmark report: " + e.getMessage());
        }
    }

    private static String truncateName(String name, int maxLen) {
        return name.length() <= maxLen ? name : name.substring(0, maxLen - 2) + "..";
    }
}
