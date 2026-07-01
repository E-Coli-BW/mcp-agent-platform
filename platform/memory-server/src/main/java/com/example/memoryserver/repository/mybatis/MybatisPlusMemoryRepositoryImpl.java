package com.example.memoryserver.repository.mybatis;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.repository.MemoryRepository;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

/**
 * MyBatis-Plus backed implementation of MemoryRepository.
 * Active when profile is "mybatis".
 */
@Repository
@Profile("mybatis")
public class MybatisPlusMemoryRepositoryImpl implements MemoryRepository {

    private final MemoryMapper mapper;

    public MybatisPlusMemoryRepositoryImpl(MemoryMapper mapper) {
        this.mapper = mapper;
    }

    @Override
    public Optional<MemoryEntity> findByTenantIdAndKey(String tenantId, String key) {
        return Optional.ofNullable(mapper.selectOne(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .eq(MemoryEntity::getKey, key)));
    }

    @Override
    public List<MemoryEntity> findByTenantIdAndNamespace(String tenantId, String namespace) {
        return mapper.selectList(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .eq(MemoryEntity::getNamespace, namespace));
    }

    @Override
    public List<MemoryEntity> findByTenantId(String tenantId) {
        return mapper.selectList(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId));
    }

    @Override
    public long countByTenantId(String tenantId) {
        return mapper.selectCount(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId));
    }

    @Override
    public List<MemoryEntity> searchByKeyword(String tenantId, String keyword) {
        String pattern = "%" + keyword.toLowerCase() + "%";
        return mapper.selectList(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .and(w -> w
                                .like(MemoryEntity::getContent, pattern)
                                .or()
                                .like(MemoryEntity::getKey, pattern)));
    }

    @Override
    public List<MemoryEntity> fullTextSearch(String tenantId, String query, int limit) {
        // MyBatis fallback — uses LIKE (no tsvector support in MyBatis-Plus)
        return searchByKeyword(tenantId, query);
    }

    @Override
    public MemoryEntity save(MemoryEntity entity) {
        if (entity.getId() == null) {
            entity.setId(UUID.randomUUID().toString());
            mapper.insert(entity);
        } else {
            mapper.updateById(entity);
        }
        return entity;
    }

    @Override
    public java.util.Map<String, Long> countByNamespace(String tenantId) {
        var result = new java.util.LinkedHashMap<String, Long>();
        var entries = mapper.selectList(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .select(MemoryEntity::getNamespace));
        for (var e : entries) {
            result.merge(e.getNamespace(), 1L, Long::sum);
        }
        return result;
    }

    @Override
    public java.util.List<MemoryEntity> findRecentByTenantId(String tenantId, int limit) {
        return mapper.selectList(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .orderByDesc(MemoryEntity::getUpdatedAt)
                        .last("LIMIT " + limit));
    }

    @Override
    public void delete(MemoryEntity entity) {
        mapper.deleteById(entity.getId());
    }

    @Override
    public int deleteByTenantIdAndKey(String tenantId, String key) {
        return mapper.delete(
                new LambdaQueryWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .eq(MemoryEntity::getKey, key));
    }

    @Override
    public int recordAccess(String tenantId, String key) {
        // Direct UPDATE via MP's UpdateWrapper — bypasses entity-level @Version
        // for the same reason as the JPA impl: access_count is monotonic and
        // conflict-free, the optimistic-lock guard is pointless here.
        return mapper.update(null,
                new com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper<MemoryEntity>()
                        .eq(MemoryEntity::getTenantId, tenantId)
                        .eq(MemoryEntity::getKey, key)
                        .setSql("access_count = access_count + 1")
                        .set(MemoryEntity::getLastAccessedAt, java.time.Instant.now()));
    }
}
