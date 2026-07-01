package com.example.memoryserver.cache;

import com.example.memoryserver.model.MemoryEntity;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * Schedules cache operations to run AFTER the current transaction commits.
 *
 * Why: If cache.put() runs inside @Transactional and Redis fails,
 * it throws an exception that rolls back the DB transaction — losing data.
 * By deferring cache writes to after-commit, DB consistency is guaranteed.
 *
 * If no transaction is active (e.g., read-only path), executes immediately.
 */
@Component
public class CacheAfterCommitExecutor {

    private static final Logger log = LoggerFactory.getLogger(CacheAfterCommitExecutor.class);

    private final MemoryCache cache;

    public CacheAfterCommitExecutor(MemoryCache cache) {
        this.cache = cache;
    }

    public void putEntry(String tenantId, MemoryEntity entity) {
        executeAfterCommit(() -> {
            try {
                cache.putEntry(tenantId, entity);
            } catch (Exception e) {
                log.warn("Cache put failed after commit (tenant={}, key={}): {}",
                        tenantId, entity.getKey(), e.getMessage());
                // Swallow — DB is source of truth, cache is best-effort
            }
        });
    }

    public void evictEntry(String tenantId, String key) {
        executeAfterCommit(() -> {
            try {
                cache.evictEntry(tenantId, key);
            } catch (Exception e) {
                log.warn("Cache evict failed after commit (tenant={}, key={}): {}",
                        tenantId, key, e.getMessage());
            }
        });
    }

    public void evictContext(String tenantId) {
        executeAfterCommit(() -> {
            try {
                cache.putContext(tenantId, null);
            } catch (Exception e) {
                log.warn("Cache context evict failed: {}", e.getMessage());
            }
        });
    }

    private void executeAfterCommit(Runnable action) {
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override
                        public void afterCommit() {
                            action.run();
                        }
                    });
        } else {
            // No active TX — execute immediately (e.g., read-only context)
            action.run();
        }
    }
}
