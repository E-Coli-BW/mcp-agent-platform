package com.example.memoryserver.repository.jpa;

import com.example.memoryserver.model.SkillEntity;
import com.example.memoryserver.repository.SkillRepository;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

/**
 * JPA-backed implementation of SkillRepository.
 */
@Repository
@Profile("!mybatis")
public class JpaSkillRepositoryImpl implements SkillRepository {

    private final JpaSkillEntityRepository jpa;

    public JpaSkillRepositoryImpl(JpaSkillEntityRepository jpa) {
        this.jpa = jpa;
    }

    @Override
    public Optional<SkillEntity> findActiveByTenantIdAndKey(String tenantId, String key) {
        return jpa.findActiveByTenantIdAndKey(tenantId, key);
    }

    @Override
    public Optional<SkillEntity> findByTenantIdAndKeyAndVersion(String tenantId, String key, int version) {
        return jpa.findByTenantIdAndKeyAndVersion(tenantId, key, version);
    }

    @Override
    public List<SkillEntity> findAllByTenantIdAndKey(String tenantId, String key) {
        return jpa.findAllByTenantIdAndKey(tenantId, key);
    }

    @Override
    public List<SkillEntity> findActiveByTenantId(String tenantId) {
        return jpa.findActiveByTenantId(tenantId);
    }

    @Override
    public List<SkillEntity> findActiveByTenantIdAndCategory(String tenantId, String category) {
        return jpa.findActiveByTenantIdAndCategory(tenantId, category);
    }

    @Override
    public List<SkillEntity> findActiveWithTriggers(String tenantId) {
        return jpa.findActiveWithTriggers(tenantId);
    }

    @Override
    public int getMaxVersion(String tenantId, String key) {
        return jpa.getMaxVersion(tenantId, key);
    }

    @Override
    public SkillEntity save(SkillEntity entity) {
        return jpa.save(entity);
    }

    @Override
    public int deprecateByTenantIdAndKey(String tenantId, String key) {
        return jpa.deprecateByTenantIdAndKey(tenantId, key);
    }
}
