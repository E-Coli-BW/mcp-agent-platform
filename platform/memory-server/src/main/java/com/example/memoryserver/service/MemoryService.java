package com.example.memoryserver.service;

import com.example.memoryserver.cache.CacheAfterCommitExecutor;
import com.example.memoryserver.cache.MemoryCache;
import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.model.dto.MemoryRequest;
import com.example.memoryserver.repository.MemoryRepository;
import com.example.memoryserver.search.MemorySearchEngine;
import com.example.memoryserver.search.MemorySearchEngine.ScoredResult;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.dao.OptimisticLockingFailureException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.*;
import java.util.stream.Collectors;

/**
 * Core business logic for memory operations.
 * Orchestrates Repository (DB), Cache (Redis), and Search (TF-IDF).
 *
 * Transaction design:
 * - All write methods use @Transactional(rollbackFor = Exception.class)
 * - Cache writes are deferred to AFTER_COMMIT via CacheAfterCommitExecutor
 * - Optimistic lock conflicts are retried up to 3 times
 * - Read-only methods use @Transactional(readOnly = true) for connection hints
 */
@Service
public class MemoryService {

    private static final Logger log = LoggerFactory.getLogger(MemoryService.class);
    private static final int MAX_ENTRIES_PER_TENANT = 10_000;
    private static final int MAX_RETRIES = 3;

    private final MemoryRepository repository;
    private final MemoryCache cache;
    private final CacheAfterCommitExecutor cacheAfterCommit;
    private final MemorySearchEngine searchEngine;
    private final MemoryWriteService writeService;
    private final ObjectMapper mapper;

    public MemoryService(MemoryRepository repository,
                         MemoryCache cache,
                         CacheAfterCommitExecutor cacheAfterCommit,
                         MemorySearchEngine searchEngine,
                         MemoryWriteService writeService,
                         ObjectMapper mapper) {
        this.repository = repository;
        this.cache = cache;
        this.cacheAfterCommit = cacheAfterCommit;
        this.searchEngine = searchEngine;
        this.writeService = writeService;
        this.mapper = mapper;
    }

    // ── Get ──────────────────────────────────────────────────────

    /**
     * Get a memory by key.
     *
     * <p>Read path is intentionally {@code readOnly = true} — recording the
     * access (a counter bump + timestamp) is done via a direct
     * {@link MemoryRepository#recordAccess} UPDATE that bypasses
     * Hibernate's first-level cache and the {@code @Version} guard.</p>
     *
     * <p>Why: the old implementation did {@code entity.recordAccess(); save(entity)}
     * inside a read-write transaction. On hot keys this turned every GET into
     * a SELECT + UPDATE pair whose {@code @Version} check would race against
     * itself, sending threads into the retry loop in {@link #set}. The fix
     * separates the two write semantics:</p>
     * <ul>
     *   <li>{@code content} updates need {@code @Version} (last-writer-wins is wrong)</li>
     *   <li>{@code accessCount} updates are monotonic and conflict-free
     *       — a direct {@code UPDATE ... access_count + 1} is correct and
     *       cheaper than a managed-entity round-trip.</li>
     * </ul>
     *
     * <p>The repository load is still inside a {@code readOnly} transaction so
     * Hibernate can use a read-only JDBC connection / replica routing.
     * The {@code recordAccess} UPDATE opens its own short transaction (via
     * {@code @Transactional} on the repository method) so it doesn't promote
     * the outer one to read-write.</p>
     */
    @Transactional(readOnly = true)
    public Optional<MemoryEntity> get(String tenantId, String key) {
        Optional<MemoryEntity> found = repository.findByTenantIdAndKey(tenantId, key);
        if (found.isEmpty()) {
            return found;
        }
        // Bump access counter in a separate, short, conflict-free UPDATE.
        // Don't fail the GET if the bump fails — it's pure telemetry.
        try {
            repository.recordAccess(tenantId, key);
        } catch (Exception e) {
            log.debug("recordAccess failed for tenant={}, key={}: {}",
                    tenantId, key, e.getMessage());
        }
        cacheAfterCommit.putEntry(tenantId, found.get());
        return found;
    }

    // ── Set ──────────────────────────────────────────────────────

    /**
     * Create or update a memory entry.
     * Retries on optimistic lock failure.
     * Cache updated only after successful commit.
     */
    public MemoryEntity set(String tenantId, MemoryRequest request) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                return writeService.doSet(tenantId, request);
            } catch (OptimisticLockingFailureException e) {
                log.warn("Optimistic lock conflict on set (tenant={}, key={}, attempt={}/{})",
                        tenantId, request.key(), attempt, MAX_RETRIES);
                if (attempt == MAX_RETRIES) {
                    throw new MemoryConflictException(
                            "Concurrent update conflict for key '" + request.key() + "' after " + MAX_RETRIES + " retries", e);
                }
                backoff(attempt);
            } catch (DataIntegrityViolationException e) {
                // Race condition: two concurrent inserts for same tenant+key
                log.warn("Duplicate key race on set (tenant={}, key={}), retrying as update", tenantId, request.key());
                if (attempt == MAX_RETRIES) {
                    throw new MemoryConflictException(
                            "Duplicate key conflict for '" + request.key() + "' after " + MAX_RETRIES + " retries", e);
                }
                backoff(attempt);
            }
        }
        throw new IllegalStateException("Unreachable");
    }

    // ── Delete ───────────────────────────────────────────────────

    /**
     * Delete by tenant+key using direct query.
     * Does NOT load the entity first — avoids @Version conflicts
     * when get() was called before delete() in the same session.
     */
    @Transactional(rollbackFor = Exception.class)
    public boolean delete(String tenantId, String key) {
        int deleted = repository.deleteByTenantIdAndKey(tenantId, key);
        if (deleted > 0) {
            cacheAfterCommit.evictEntry(tenantId, key);
            cacheAfterCommit.evictContext(tenantId);
            return true;
        }
        return false;
    }

    // ── Pin ──────────────────────────────────────────────────────

    public Optional<MemoryEntity> pin(String tenantId, String key, boolean pinned) {
        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                return writeService.doPin(tenantId, key, pinned);
            } catch (OptimisticLockingFailureException e) {
                log.warn("Optimistic lock conflict on pin (tenant={}, key={}, attempt={}/{})",
                        tenantId, key, attempt, MAX_RETRIES);
                if (attempt == MAX_RETRIES) {
                    throw new MemoryConflictException("Pin conflict for '" + key + "'", e);
                }
            }
        }
        throw new IllegalStateException("Unreachable");
    }

    // ── Search ───────────────────────────────────────────────────

    @Transactional(readOnly = true)
    public List<ScoredResult> search(String tenantId, String query,
                                     List<String> tags, String namespace, int limit) {
        List<MemoryEntity> entries;

        // Strategy: try DB-side full-text search first for efficiency,
        // fall back to in-memory TF-IDF if tsvector is unavailable (H2).
        if (namespace != null && !namespace.isEmpty()) {
            entries = repository.findByTenantIdAndNamespace(tenantId, namespace);
        } else {
            // Try full-text search (PostgreSQL only), fall back to full load
            entries = safeFullTextSearch(tenantId, query, Math.max(limit * 5, 100));
            if (entries == null) {
                entries = repository.findByTenantId(tenantId);
            }
        }

        return searchEngine.search(entries, query, tags, limit);
    }

    // ── Context ──────────────────────────────────────────────────

    @Transactional(readOnly = true)
    public String context(String tenantId) {
        Optional<String> cached = safeGetContext(tenantId);
        if (cached.isPresent()) return cached.get();

        long totalCount = repository.countByTenantId(tenantId);
        if (totalCount == 0) {
            return toJson(Map.of("status", "empty", "totalMemories", 0,
                    "suggestion", "No memories yet. Use memory_set to store information."));
        }

        // SQL aggregation — no full table scan
        Map<String, Long> nsCounts = repository.countByNamespace(tenantId);

        // Only load the 10 most recent entries (not all)
        List<MemoryEntity> recent = repository.findRecentByTenantId(tenantId, 10);

        // Tag counts from recent entries only (approximation, avoids full scan)
        Map<String, Integer> tagCounts = new LinkedHashMap<>();
        for (var e : recent) {
            for (var t : e.getTags()) tagCounts.merge(t, 1, Integer::sum);
        }

        var topTags = tagCounts.entrySet().stream()
                .sorted(Map.Entry.<String, Integer>comparingByValue().reversed())
                .limit(20)
                .map(e -> Map.of("tag", e.getKey(), "count", (Object) e.getValue()))
                .toList();

        var recentEntries = recent.stream()
                .map(e -> Map.of(
                        "key", (Object) e.getKey(),
                        "namespace", e.getNamespace(),
                        "updatedAt", e.getUpdatedAt().toString(),
                        "preview", e.getContent().length() > 80
                                ? e.getContent().substring(0, 80) : e.getContent(),
                        "pinned", e.isPinned()))
                .toList();

        String result = toJson(Map.of(
                "status", "ready",
                "totalMemories", totalCount,
                "namespaces", nsCounts,
                "topTags", topTags,
                "recentEntries", recentEntries));

        try { cache.putContext(tenantId, result); }
        catch (Exception e) { log.warn("Failed to cache context: {}", e.getMessage()); }
        return result;
    }

    // ── List ─────────────────────────────────────────────────────

    @Transactional(readOnly = true)
    public List<MemoryEntity> list(String tenantId, String namespace, Set<String> tags) {
        List<MemoryEntity> entries;
        if (namespace != null && !namespace.isEmpty()) {
            entries = repository.findByTenantIdAndNamespace(tenantId, namespace);
        } else {
            entries = repository.findByTenantId(tenantId);
        }

        if (tags != null && !tags.isEmpty()) {
            Set<String> lowerTags = tags.stream().map(String::toLowerCase).collect(Collectors.toSet());
            entries = entries.stream()
                    .filter(e -> e.getTags().stream().anyMatch(t -> lowerTags.contains(t.toLowerCase())))
                    .toList();
        }

        return entries;
    }

    // ── Helpers ──────────────────────────────────────────────────

    /**
     * Try PostgreSQL full-text search. Returns null if unavailable (H2, no tsvector).
     * On H2 (integration tests), the native query would fail and mark the
     * transaction as rollback-only, so we detect H2 and skip entirely.
     */
    /**
     * Try PostgreSQL full-text search. Returns null ONLY if the search mechanism
     * itself is unavailable (H2, no tsvector). Empty list means "no matches found"
     * and should NOT trigger a fallback to full table scan.
     */
    private List<MemoryEntity> safeFullTextSearch(String tenantId, String query, int limit) {
        try {
            return repository.fullTextSearch(tenantId, query, limit); // may be empty — that's OK
        } catch (Exception e) {
            log.debug("fullTextSearch unavailable, falling back to in-memory: {}", e.getMessage());
            return null; // null = mechanism unavailable, trigger fallback
        }
    }

    /** Cache read — swallow Redis failures, return empty */
    private Optional<MemoryEntity> safeGetFromCache(String tenantId, String key) {
        try {
            return cache.getEntry(tenantId, key);
        } catch (Exception e) {
            log.warn("Cache read failed (tenant={}, key={}): {}", tenantId, key, e.getMessage());
            return Optional.empty();
        }
    }

    private Optional<String> safeGetContext(String tenantId) {
        try {
            return cache.getContext(tenantId);
        } catch (Exception e) {
            log.warn("Cache context read failed: {}", e.getMessage());
            return Optional.empty();
        }
    }

    private String toJson(Object obj) {
        try { return mapper.writeValueAsString(obj); }
        catch (Exception e) { return "{}"; }
    }

    /** Exponential backoff with jitter: ~10ms, ~20ms, ~40ms, ... */
    private void backoff(int attempt) {
        try {
            long baseMs = 10L * (1L << (attempt - 1)); // 10, 20, 40, ...
            long jitter = (long) (Math.random() * baseMs);
            Thread.sleep(baseMs + jitter);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
