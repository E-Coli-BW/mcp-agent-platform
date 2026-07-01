package com.example.memoryserver.repository.jpa;

import com.example.memoryserver.model.SkillEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

/**
 * Spring Data JPA interface for skills — internal to JPA implementation.
 */
public interface JpaSkillEntityRepository extends JpaRepository<SkillEntity, String> {

    @Query("SELECT s FROM SkillEntity s WHERE s.tenantId = :tid AND s.key = :key AND s.status = 'active'")
    Optional<SkillEntity> findActiveByTenantIdAndKey(@Param("tid") String tenantId, @Param("key") String key);

    Optional<SkillEntity> findByTenantIdAndKeyAndVersion(String tenantId, String key, int version);

    @Query("SELECT s FROM SkillEntity s WHERE s.tenantId = :tid AND s.key = :key ORDER BY s.version DESC")
    List<SkillEntity> findAllByTenantIdAndKey(@Param("tid") String tenantId, @Param("key") String key);

    @Query("SELECT s FROM SkillEntity s WHERE s.tenantId = :tid AND s.status = 'active' ORDER BY s.updatedAt DESC")
    List<SkillEntity> findActiveByTenantId(@Param("tid") String tenantId);

    @Query("SELECT s FROM SkillEntity s WHERE s.tenantId = :tid AND s.status = 'active' AND s.category = :cat ORDER BY s.updatedAt DESC")
    List<SkillEntity> findActiveByTenantIdAndCategory(@Param("tid") String tenantId, @Param("cat") String category);

    @Query("SELECT s FROM SkillEntity s WHERE s.tenantId = :tid AND s.status = 'active' " +
           "AND (s.triggerPatterns IS NOT NULL OR s.triggerErrors IS NOT NULL OR s.triggerTools IS NOT NULL)")
    List<SkillEntity> findActiveWithTriggers(@Param("tid") String tenantId);

    @Query("SELECT COALESCE(MAX(s.version), 0) FROM SkillEntity s WHERE s.tenantId = :tid AND s.key = :key")
    int getMaxVersion(@Param("tid") String tenantId, @Param("key") String key);

    @Modifying(clearAutomatically = true)
    @Query("UPDATE SkillEntity s SET s.status = 'deprecated' " +
           "WHERE s.tenantId = :tid AND s.key = :key AND s.status = 'active'")
    int deprecateByTenantIdAndKey(@Param("tid") String tenantId, @Param("key") String key);
}
