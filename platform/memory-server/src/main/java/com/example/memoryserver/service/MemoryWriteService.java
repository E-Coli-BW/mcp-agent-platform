package com.example.memoryserver.service;

import com.example.memoryserver.cache.CacheAfterCommitExecutor;
import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.model.dto.MemoryRequest;
import com.example.memoryserver.repository.MemoryRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Optional;

/**
 * Handles transactional write operations for memory entries.
 *
 * Extracted from MemoryService to eliminate the self-injection anti-pattern.
 * MemoryService.set() and pin() call this service's @Transactional methods
 * directly, so Spring's proxy intercepts the transaction boundary correctly.
 *
 * Why self-injection was bad:
 *   @Lazy @Autowired private MemoryService self;
 *   self.doSet(...) — works but is fragile, hard to test, and a known anti-pattern.
 */
@Service
public class MemoryWriteService {

    private static final int MAX_ENTRIES_PER_TENANT = 10_000;

    private final MemoryRepository repository;
    private final CacheAfterCommitExecutor cacheAfterCommit;

    public MemoryWriteService(MemoryRepository repository,
                               CacheAfterCommitExecutor cacheAfterCommit) {
        this.repository = repository;
        this.cacheAfterCommit = cacheAfterCommit;
    }

    @Transactional(rollbackFor = Exception.class)
    public MemoryEntity doSet(String tenantId, MemoryRequest request) {
        Optional<MemoryEntity> existing = repository.findByTenantIdAndKey(tenantId, request.key());

        MemoryEntity entity;
        if (existing.isPresent()) {
            entity = existing.get();
            entity.updateContent(
                    request.content(),
                    request.resolvedNamespace(entity.getNamespace()),
                    request.tags(),
                    request.pinned());
        } else {
            // Only check quota on INSERT, not on UPDATE — avoids unnecessary COUNT query
            long count = repository.countByTenantId(tenantId);
            if (count >= MAX_ENTRIES_PER_TENANT) {
                throw new MemoryQuotaExceededException(tenantId, MAX_ENTRIES_PER_TENANT);
            }

            entity = new MemoryEntity(tenantId, request.key(), request.content(),
                    request.resolvedNamespace("default"));
            if (request.tags() != null) {
                entity.updateContent(request.content(), null, request.tags(), request.pinned());
            }
        }

        entity = repository.save(entity);
        // Only evict stale cache; new value written after commit
        cacheAfterCommit.putEntry(tenantId, entity);
        cacheAfterCommit.evictContext(tenantId);
        return entity;
    }

    @Transactional(rollbackFor = Exception.class)
    public Optional<MemoryEntity> doPin(String tenantId, String key, boolean pinned) {
        return repository.findByTenantIdAndKey(tenantId, key)
                .map(entity -> {
                    entity.updateContent(entity.getContent(), null, null, pinned);
                    entity = repository.save(entity);
                    cacheAfterCommit.putEntry(tenantId, entity);
                    return entity;
                });
    }
}
