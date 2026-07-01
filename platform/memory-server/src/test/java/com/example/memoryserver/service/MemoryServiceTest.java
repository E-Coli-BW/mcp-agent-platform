package com.example.memoryserver.service;

import com.example.memoryserver.cache.CacheAfterCommitExecutor;
import com.example.memoryserver.cache.MemoryCache;
import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.model.dto.MemoryRequest;
import com.example.memoryserver.repository.MemoryRepository;
import com.example.memoryserver.search.MemorySearchEngine;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.OptimisticLockingFailureException;

import java.util.List;
import java.util.Optional;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for MemoryService.
 * Mocks repository, cache, and search engine — tests pure business logic.
 */
@ExtendWith(MockitoExtension.class)
class MemoryServiceTest {

    @Mock private MemoryRepository repository;
    @Mock private MemoryCache cache;
    @Mock private CacheAfterCommitExecutor cacheAfterCommit;
    @Mock private MemorySearchEngine searchEngine;

    private MemoryWriteService writeService;
    private MemoryService service;
    private final ObjectMapper mapper = new ObjectMapper();

    @BeforeEach
    void setUp() {
        writeService = new MemoryWriteService(repository, cacheAfterCommit);
        service = new MemoryService(repository, cache, cacheAfterCommit, searchEngine, writeService, mapper);
    }

    // ── Get ──────────────────────────────────────────────────────

    @Test
    void get_fromDb() {
        var entity = new MemoryEntity("t1", "k1", "content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.of(entity));
        when(repository.recordAccess("t1", "k1")).thenReturn(1);

        Optional<MemoryEntity> result = service.get("t1", "k1");

        assertTrue(result.isPresent());
        assertEquals("k1", result.get().getKey());
        verify(repository).findByTenantIdAndKey("t1", "k1");
        // P0-5 fix: read path bumps the counter via a direct UPDATE,
        // not via entity.recordAccess() + save() (which would race with
        // @Version on hot keys).
        verify(repository).recordAccess("t1", "k1");
        verify(repository, never()).save(any());
        verify(cacheAfterCommit).putEntry(eq("t1"), any());
    }

    @Test
    void get_recordAccessFailure_doesNotFailGet() {
        var entity = new MemoryEntity("t1", "k1", "content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.of(entity));
        when(repository.recordAccess("t1", "k1"))
                .thenThrow(new RuntimeException("db hiccup"));

        // The counter bump is best-effort telemetry — a DB blip on the
        // access stat must NOT propagate as a 500 on a successful read.
        Optional<MemoryEntity> result = service.get("t1", "k1");
        assertTrue(result.isPresent());
    }

    @Test
    void get_notFound() {
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.empty());

        Optional<MemoryEntity> result = service.get("t1", "k1");

        assertTrue(result.isEmpty());
        // No counter bump for a miss
        verify(repository, never()).recordAccess(anyString(), anyString());
    }

    // ── Set ──────────────────────────────────────────────────────

    @Test
    void set_createsNewEntry() {
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.empty());
        when(repository.countByTenantId("t1")).thenReturn(0L);
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        var request = new MemoryRequest("k1", "hello world", null, null, null);
        MemoryEntity result = service.set("t1", request);

        assertNotNull(result);
        assertEquals("k1", result.getKey());
        assertEquals("hello world", result.getContent());
        verify(repository).save(any());
    }

    @Test
    void set_updatesExistingEntry() {
        var existing = new MemoryEntity("t1", "k1", "old content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.of(existing));
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        var request = new MemoryRequest("k1", "new content", null, Set.of("updated"), null);
        MemoryEntity result = service.set("t1", request);

        assertEquals("new content", result.getContent());
        assertTrue(result.getTags().contains("updated"));
    }

    @Test
    void set_quotaExceeded_throws() {
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.empty());
        when(repository.countByTenantId("t1")).thenReturn(10_000L);

        var request = new MemoryRequest("k1", "content", null, null, null);

        assertThrows(MemoryQuotaExceededException.class, () -> service.set("t1", request));
    }

    @Test
    void set_optimisticLockRetry_succeeds() {
        // First call throws OptimisticLockingFailureException, second succeeds
        var entity = new MemoryEntity("t1", "k1", "content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1"))
                .thenReturn(Optional.of(entity));
        when(repository.save(any()))
                .thenThrow(new OptimisticLockingFailureException("conflict"))
                .thenAnswer(inv -> inv.getArgument(0));

        var request = new MemoryRequest("k1", "updated", null, null, null);
        MemoryEntity result = service.set("t1", request);

        assertNotNull(result);
        verify(repository, times(2)).save(any());
    }

    @Test
    void set_optimisticLockRetry_exhausted_throws() {
        var entity = new MemoryEntity("t1", "k1", "content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1"))
                .thenReturn(Optional.of(entity));
        when(repository.save(any()))
                .thenThrow(new OptimisticLockingFailureException("conflict"));

        var request = new MemoryRequest("k1", "updated", null, null, null);

        assertThrows(MemoryConflictException.class, () -> service.set("t1", request));
        verify(repository, times(3)).save(any());
    }

    // ── Delete ───────────────────────────────────────────────────

    @Test
    void delete_existing() {
        when(repository.deleteByTenantIdAndKey("t1", "k1")).thenReturn(1);

        assertTrue(service.delete("t1", "k1"));
        verify(repository).deleteByTenantIdAndKey("t1", "k1");
        verify(cacheAfterCommit).evictEntry("t1", "k1");
    }

    @Test
    void delete_notFound() {
        when(repository.deleteByTenantIdAndKey("t1", "k1")).thenReturn(0);

        assertFalse(service.delete("t1", "k1"));
    }

    // ── Pin ──────────────────────────────────────────────────────

    @Test
    void pin_existing() {
        var entity = new MemoryEntity("t1", "k1", "content", "default");
        when(repository.findByTenantIdAndKey("t1", "k1")).thenReturn(Optional.of(entity));
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        Optional<MemoryEntity> result = service.pin("t1", "k1", true);

        assertTrue(result.isPresent());
        assertTrue(result.get().isPinned());
    }

    // ── List ─────────────────────────────────────────────────────

    @Test
    void list_filtersByTags() {
        var e1 = new MemoryEntity("t1", "k1", "c1", "default");
        e1.updateContent("c1", null, Set.of("java"), null);
        var e2 = new MemoryEntity("t1", "k2", "c2", "default");
        e2.updateContent("c2", null, Set.of("python"), null);

        when(repository.findByTenantId("t1")).thenReturn(List.of(e1, e2));

        List<MemoryEntity> result = service.list("t1", null, Set.of("java"));

        assertEquals(1, result.size());
        assertEquals("k1", result.get(0).getKey());
    }

    // ── Context ──────────────────────────────────────────────────

    @Test
    void context_empty() {
        when(cache.getContext("t1")).thenReturn(Optional.empty());
        when(repository.countByTenantId("t1")).thenReturn(0L);

        String result = service.context("t1");

        assertTrue(result.contains("empty"));
    }

    @Test
    void context_withEntries() {
        when(cache.getContext("t1")).thenReturn(Optional.empty());
        when(repository.countByTenantId("t1")).thenReturn(1L);
        when(repository.countByNamespace("t1")).thenReturn(java.util.Map.of("ns1", 1L));
        var e1 = new MemoryEntity("t1", "k1", "hello world content", "ns1");
        e1.updateContent("hello world content", null, Set.of("tag1"), false);
        when(repository.findRecentByTenantId("t1", 10)).thenReturn(List.of(e1));

        String result = service.context("t1");

        assertTrue(result.contains("ready"));
        assertTrue(result.contains("ns1"));
    }
}
