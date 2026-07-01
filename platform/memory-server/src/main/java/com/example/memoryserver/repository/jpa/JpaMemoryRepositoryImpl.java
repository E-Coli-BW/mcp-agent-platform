package com.example.memoryserver.repository.jpa;

import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.repository.MemoryRepository;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/**
 * JPA-backed implementation of MemoryRepository.
 * Active when profile is "jpa" or no profile is set (default).
 */
@Repository
@Profile("!mybatis")
public class JpaMemoryRepositoryImpl implements MemoryRepository {

    private final JpaMemoryEntityRepository jpaRepo;

    public JpaMemoryRepositoryImpl(JpaMemoryEntityRepository jpaRepo) {
        this.jpaRepo = jpaRepo;
    }

    @Override
    public Optional<MemoryEntity> findByTenantIdAndKey(String tenantId, String key) {
        return jpaRepo.findByTenantIdAndKey(tenantId, key);
    }

    @Override
    public List<MemoryEntity> findByTenantIdAndNamespace(String tenantId, String namespace) {
        return jpaRepo.findByTenantIdAndNamespace(tenantId, namespace);
    }

    @Override
    public List<MemoryEntity> findByTenantId(String tenantId) {
        return jpaRepo.findByTenantId(tenantId);
    }

    @Override
    public long countByTenantId(String tenantId) {
        return jpaRepo.countByTenantId(tenantId);
    }

    @Override
    public List<MemoryEntity> searchByKeyword(String tenantId, String keyword) {
        return jpaRepo.searchByKeyword(tenantId, keyword);
    }

    @Override
    @org.springframework.transaction.annotation.Transactional(
            readOnly = true,
            propagation = org.springframework.transaction.annotation.Propagation.REQUIRES_NEW,
            noRollbackFor = Exception.class)
    public List<MemoryEntity> fullTextSearch(String tenantId, String query, int limit) {
        try {
            return jpaRepo.fullTextSearch(tenantId, query, limit);
        } catch (Exception e) {
            // H2 doesn't support tsvector — fall back to LIKE search
            return jpaRepo.searchByKeyword(tenantId, query).stream()
                    .limit(limit)
                    .toList();
        }
    }

    @Override
    public MemoryEntity save(MemoryEntity entity) {
        return jpaRepo.save(entity);
    }

    @Override
    public java.util.Map<String, Long> countByNamespace(String tenantId) {
        var result = new java.util.LinkedHashMap<String, Long>();
        for (Object[] row : jpaRepo.countByNamespace(tenantId)) {
            result.put((String) row[0], (Long) row[1]);
        }
        return result;
    }

    @Override
    public List<MemoryEntity> findRecentByTenantId(String tenantId, int limit) {
        return jpaRepo.findRecentByTenantId(tenantId,
                org.springframework.data.domain.PageRequest.of(0, limit));
    }

    @Override
    public void delete(MemoryEntity entity) {
        jpaRepo.delete(entity);
    }

    @Override
    public int deleteByTenantIdAndKey(String tenantId, String key) {
        return jpaRepo.deleteByTenantIdAndKey(tenantId, key);
    }

    @Override
    @org.springframework.transaction.annotation.Transactional
    public int recordAccess(String tenantId, String key) {
        // CURRENT_TIMESTAMP in HQL returns java.sql.Timestamp, but
        // MemoryEntity.lastAccessedAt is java.time.Instant. Hibernate 6
        // refuses the implicit conversion (SemanticException). Pass an
        // explicit Instant parameter — also lets tests inject a fixed clock.
        return jpaRepo.recordAccess(tenantId, key, java.time.Instant.now());
    }
}
