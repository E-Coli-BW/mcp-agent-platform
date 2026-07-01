package com.example.memoryserver.repository.jpa;

import com.example.memoryserver.model.MemoryEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

/**
 * Spring Data JPA interface — internal to the JPA implementation.
 * Not exposed directly; wrapped by JpaMemoryRepositoryImpl.
 */
public interface JpaMemoryEntityRepository extends JpaRepository<MemoryEntity, String> {

    Optional<MemoryEntity> findByTenantIdAndKey(String tenantId, String key);

    List<MemoryEntity> findByTenantIdAndNamespace(String tenantId, String namespace);

    List<MemoryEntity> findByTenantId(String tenantId);

    long countByTenantId(String tenantId);

    @Query("SELECT m FROM MemoryEntity m WHERE m.tenantId = :tid " +
           "AND (LOWER(m.content) LIKE LOWER(CONCAT('%', :kw, '%')) " +
           "  OR LOWER(m.key) LIKE LOWER(CONCAT('%', :kw, '%')))")
    List<MemoryEntity> searchByKeyword(@Param("tid") String tenantId, @Param("kw") String keyword);

    /**
     * PostgreSQL full-text search using tsvector + GIN index.
     * Much faster than LIKE for large datasets (O(log n) vs O(n)).
     * Falls back to searchByKeyword on H2 (no tsvector support).
     */
    @Query(value = "SELECT * FROM memories WHERE tenant_id = :tid " +
           "AND search_vector @@ plainto_tsquery('english', :query) " +
           "ORDER BY ts_rank(search_vector, plainto_tsquery('english', :query)) DESC " +
           "LIMIT :lim",
           nativeQuery = true)
    List<MemoryEntity> fullTextSearch(@Param("tid") String tenantId,
                                      @Param("query") String query,
                                      @Param("lim") int limit);

    /** Direct JPQL delete — bypasses @Version, returns affected row count */
    @Modifying(clearAutomatically = true)
    @Query("DELETE FROM MemoryEntity m WHERE m.tenantId = :tid AND m.key = :key")
    int deleteByTenantIdAndKey(@Param("tid") String tenantId, @Param("key") String key);

    /**
     * Atomic access-counter bump — bypasses @Version on purpose.
     *
     * <p>Why a direct UPDATE instead of {@code entity.recordAccess(); save()}?
     * The accessCount field is monotonically incrementing and conflict-free:
     * two concurrent readers don't "overwrite" each other, they both want
     * to add 1. Going through Hibernate's first-level cache + {@code @Version}
     * check turned every hot-key GET into:</p>
     * <pre>
     *   T1: SELECT ... v=5     T2: SELECT ... v=5
     *   T1: UPDATE ... v=6     T2: UPDATE ... v=6 → OptimisticLockException
     *   T2: retry              ... thundering retries on the hottest keys
     * </pre>
     * <p>Direct SQL serialises on the row lock for ~microseconds and never
     * collides with the {@code @Version} guard on content updates because
     * we only touch {@code access_count} + {@code last_accessed_at}.</p>
     *
     * @return number of rows updated (0 if the entry was deleted concurrently)
     */
    @Modifying(clearAutomatically = true, flushAutomatically = true)
    @Query("UPDATE MemoryEntity m " +
           "SET m.accessCount = m.accessCount + 1, m.lastAccessedAt = :now " +
           "WHERE m.tenantId = :tid AND m.key = :key")
    int recordAccess(@Param("tid") String tenantId, @Param("key") String key, @Param("now") java.time.Instant now);

    /** Count entries grouped by namespace — for context() aggregation without full scan */
    @Query("SELECT m.namespace, COUNT(m) FROM MemoryEntity m WHERE m.tenantId = :tid GROUP BY m.namespace")
    List<Object[]> countByNamespace(@Param("tid") String tenantId);

    /** Get N most recently updated entries — for context() without loading all */
    @Query("SELECT m FROM MemoryEntity m WHERE m.tenantId = :tid ORDER BY m.updatedAt DESC")
    List<MemoryEntity> findRecentByTenantId(@Param("tid") String tenantId, org.springframework.data.domain.Pageable pageable);
}
