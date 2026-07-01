package com.example.memoryserver.service;

import com.example.memoryserver.model.SkillEntity;
import com.example.memoryserver.repository.SkillRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.*;

/**
 * Core business logic for skill operations.
 *
 * Design invariants:
 * - Only ONE version per (tenant, key) has status='active' at any time.
 * - skill_set on existing key: deprecates current active, inserts version+1.
 * - tenantId is always the first parameter (multi-tenancy contract).
 */
@Service
public class SkillService {

    private static final Logger log = LoggerFactory.getLogger(SkillService.class);
    private static final int MAX_SKILLS_PER_TENANT = 500;

    private final SkillRepository repository;

    public SkillService(SkillRepository repository) {
        this.repository = repository;
    }

    // ── Get ──────────────────────────────────────────────────────

    @Transactional(readOnly = true)
    public Optional<SkillEntity> getActive(String tenantId, String key) {
        return repository.findActiveByTenantIdAndKey(tenantId, key);
    }

    @Transactional(readOnly = true)
    public Optional<SkillEntity> getVersion(String tenantId, String key, int version) {
        return repository.findByTenantIdAndKeyAndVersion(tenantId, key, version);
    }

    @Transactional(readOnly = true)
    public List<SkillEntity> getHistory(String tenantId, String key) {
        return repository.findAllByTenantIdAndKey(tenantId, key);
    }

    // ── List ─────────────────────────────────────────────────────

    @Transactional(readOnly = true)
    public List<SkillEntity> listActive(String tenantId) {
        return repository.findActiveByTenantId(tenantId);
    }

    @Transactional(readOnly = true)
    public List<SkillEntity> listActiveByCategory(String tenantId, String category) {
        return repository.findActiveByTenantIdAndCategory(tenantId, category);
    }

    // ── Triggers (for cache loading) ─────────────────────────────

    @Transactional(readOnly = true)
    public List<SkillEntity> getActiveWithTriggers(String tenantId) {
        return repository.findActiveWithTriggers(tenantId);
    }

    // ── Set (create or version-up) ───────────────────────────────

    @Transactional(rollbackFor = Exception.class)
    public SkillEntity set(String tenantId, String key, String title, String problem,
                           String steps, String category, String preconditions,
                           String expectedOutcome, String pitfalls,
                           String triggerPatterns, String triggerTools, String triggerErrors,
                           String dependsOn, Set<String> tags, String createdBy) {

        // Deprecate current active version (if any)
        repository.deprecateByTenantIdAndKey(tenantId, key);

        int nextVersion = repository.getMaxVersion(tenantId, key) + 1;

        SkillEntity entity = new SkillEntity(tenantId, key, nextVersion, title, problem, steps);
        entity.setCategory(category);
        entity.setPreconditions(preconditions);
        entity.setExpectedOutcome(expectedOutcome);
        entity.setPitfalls(pitfalls);
        entity.setTriggerPatterns(triggerPatterns);
        entity.setTriggerTools(triggerTools);
        entity.setTriggerErrors(triggerErrors);
        entity.setDependsOn(dependsOn);
        if (tags != null) {
            entity.setTags(tags);
        }
        entity.setCreatedBy(createdBy);

        SkillEntity saved = repository.save(entity);
        log.info("✅ Skill set: tenant={}, key={}, version={}", tenantId, key, nextVersion);
        return saved;
    }

    // ── Rollback ─────────────────────────────────────────────────

    @Transactional(rollbackFor = Exception.class)
    public Optional<SkillEntity> rollback(String tenantId, String key, int targetVersion) {
        Optional<SkillEntity> target = repository.findByTenantIdAndKeyAndVersion(tenantId, key, targetVersion);
        if (target.isEmpty()) {
            return Optional.empty();
        }

        // Deprecate current active
        repository.deprecateByTenantIdAndKey(tenantId, key);

        // Re-activate target
        SkillEntity entity = target.get();
        entity.activate();
        repository.save(entity);

        log.info("🔄 Skill rolled back: tenant={}, key={}, to version={}", tenantId, key, targetVersion);
        return Optional.of(entity);
    }

    // ── Feedback ─────────────────────────────────────────────────

    @Transactional(rollbackFor = Exception.class)
    public Optional<SkillEntity> recordFeedback(String tenantId, String key, boolean success) {
        return repository.findActiveByTenantIdAndKey(tenantId, key)
                .map(entity -> {
                    entity.recordUse(success);
                    return repository.save(entity);
                });
    }
}
