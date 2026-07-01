package com.example.memoryserver.repository;

import com.example.memoryserver.model.MemoryEntity;

import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Persistence SPI for memory entries.
 * Implementations: JpaMemoryRepositoryImpl (JPA), MybatisPlusMemoryRepositoryImpl (MyBatis-Plus).
 * Switch via Spring profile: spring.profiles.active=jpa (default) or mybatis.
 */
public interface MemoryRepository {

    Optional<MemoryEntity> findByTenantIdAndKey(String tenantId, String key);

    List<MemoryEntity> findByTenantIdAndNamespace(String tenantId, String namespace);

    List<MemoryEntity> findByTenantId(String tenantId);

    long countByTenantId(String tenantId);

    List<MemoryEntity> searchByKeyword(String tenantId, String keyword);

    /** PostgreSQL full-text search. Falls back to searchByKeyword on H2. */
    List<MemoryEntity> fullTextSearch(String tenantId, String query, int limit);

    /** Count entries grouped by namespace. For context() aggregation without full scan. */
    Map<String, Long> countByNamespace(String tenantId);

    /** Get N most recently updated entries. For context() without full scan. */
    List<MemoryEntity> findRecentByTenantId(String tenantId, int limit);

    MemoryEntity save(MemoryEntity entity);

    void delete(MemoryEntity entity);

    /** Returns number of rows deleted (0 or 1) */
    int deleteByTenantIdAndKey(String tenantId, String key);

    /**
     * Atomically bump access_count + last_accessed_at without going through
     * {@code @Version}. Used by the read path to avoid optimistic-lock storms
     * on hot keys (see {@code MemoryService.get()}).
     *
     * @return rows updated (0 if the entry was deleted concurrently)
     */
    int recordAccess(String tenantId, String key);
}
