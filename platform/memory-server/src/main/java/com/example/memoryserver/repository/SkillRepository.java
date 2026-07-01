package com.example.memoryserver.repository;

import com.example.memoryserver.model.SkillEntity;

import java.util.List;
import java.util.Optional;

/**
 * Persistence SPI for skill entries.
 * Follows same pattern as MemoryRepository — interface + JPA impl.
 */
public interface SkillRepository {

    Optional<SkillEntity> findActiveByTenantIdAndKey(String tenantId, String key);

    Optional<SkillEntity> findByTenantIdAndKeyAndVersion(String tenantId, String key, int version);

    List<SkillEntity> findAllByTenantIdAndKey(String tenantId, String key);

    List<SkillEntity> findActiveByTenantId(String tenantId);

    List<SkillEntity> findActiveByTenantIdAndCategory(String tenantId, String category);

    /** Returns all active skills that have trigger metadata (for cache loading). */
    List<SkillEntity> findActiveWithTriggers(String tenantId);

    int getMaxVersion(String tenantId, String key);

    SkillEntity save(SkillEntity entity);

    int deprecateByTenantIdAndKey(String tenantId, String key);
}
