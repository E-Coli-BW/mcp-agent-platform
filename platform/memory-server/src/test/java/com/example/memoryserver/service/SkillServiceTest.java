package com.example.memoryserver.service;

import com.example.memoryserver.model.SkillEntity;
import com.example.memoryserver.repository.SkillRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;
import java.util.Optional;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for SkillService.
 * Validates versioning, rollback, feedback, and invariants.
 */
@ExtendWith(MockitoExtension.class)
class SkillServiceTest {

    @Mock private SkillRepository repository;

    private SkillService service;

    private static final String TENANT = "tenant-1";
    private static final String KEY = "maven-stale-jar-fix";

    @BeforeEach
    void setUp() {
        service = new SkillService(repository);
    }

    // ── Set (create new) ─────────────────────────────────────────

    @Test
    void should_createNewSkill_when_keyDoesNotExist() {
        when(repository.deprecateByTenantIdAndKey(TENANT, KEY)).thenReturn(0);
        when(repository.getMaxVersion(TENANT, KEY)).thenReturn(0);
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        SkillEntity result = service.set(TENANT, KEY, "Fix stale JAR",
                "ClassNotFoundException after mcp-common edits",
                "[{\"order\":1,\"action\":\"run mvn install\"}]",
                "build", null, null, null, null, null, null, null,
                Set.of("build", "maven"), "agent");

        assertEquals(KEY, result.getKey());
        assertEquals(1, result.getVersion());
        assertEquals("active", result.getStatus());
        assertEquals("build", result.getCategory());
        assertEquals(Set.of("build", "maven"), result.getTags());
        assertEquals("agent", result.getCreatedBy());

        verify(repository).deprecateByTenantIdAndKey(TENANT, KEY);
        verify(repository).save(any());
    }

    @Test
    void should_versionUp_when_keyAlreadyExists() {
        // Existing skill at version 2
        when(repository.deprecateByTenantIdAndKey(TENANT, KEY)).thenReturn(1);
        when(repository.getMaxVersion(TENANT, KEY)).thenReturn(2);
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        SkillEntity result = service.set(TENANT, KEY, "Updated title",
                "Updated problem", "[{\"order\":1,\"action\":\"new steps\"}]",
                "build", null, null, null, null, null, null, null,
                Set.of("build"), "agent");

        assertEquals(3, result.getVersion());
        assertEquals("active", result.getStatus());
        verify(repository).deprecateByTenantIdAndKey(TENANT, KEY);
    }

    @Test
    void should_storeTriggerMetadata_when_provided() {
        when(repository.deprecateByTenantIdAndKey(TENANT, KEY)).thenReturn(0);
        when(repository.getMaxVersion(TENANT, KEY)).thenReturn(0);
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        String triggerPatterns = "[{\"type\":\"keyword\",\"terms\":[\"stale jar\"]}]";
        String triggerErrors = "[{\"pattern\":\"ClassNotFoundException\"}]";
        String triggerTools = "[\"code_shell\"]";

        SkillEntity result = service.set(TENANT, KEY, "Title", "Problem", "[]",
                null, null, null, null,
                triggerPatterns, triggerTools, triggerErrors, null,
                Set.of(), "agent");

        assertEquals(triggerPatterns, result.getTriggerPatterns());
        assertEquals(triggerErrors, result.getTriggerErrors());
        assertEquals(triggerTools, result.getTriggerTools());
    }

    // ── Get ──────────────────────────────────────────────────────

    @Test
    void should_returnActive_when_keyExists() {
        SkillEntity entity = makeSkill(KEY, 2, "active");
        when(repository.findActiveByTenantIdAndKey(TENANT, KEY)).thenReturn(Optional.of(entity));

        Optional<SkillEntity> result = service.getActive(TENANT, KEY);

        assertTrue(result.isPresent());
        assertEquals(2, result.get().getVersion());
    }

    @Test
    void should_returnEmpty_when_keyNotFound() {
        when(repository.findActiveByTenantIdAndKey(TENANT, "nonexistent")).thenReturn(Optional.empty());

        Optional<SkillEntity> result = service.getActive(TENANT, "nonexistent");

        assertTrue(result.isEmpty());
    }

    @Test
    void should_returnSpecificVersion_when_versionProvided() {
        SkillEntity v1 = makeSkill(KEY, 1, "deprecated");
        when(repository.findByTenantIdAndKeyAndVersion(TENANT, KEY, 1)).thenReturn(Optional.of(v1));

        Optional<SkillEntity> result = service.getVersion(TENANT, KEY, 1);

        assertTrue(result.isPresent());
        assertEquals(1, result.get().getVersion());
    }

    // ── History ──────────────────────────────────────────────────

    @Test
    void should_returnAllVersions_when_historyRequested() {
        List<SkillEntity> versions = List.of(
                makeSkill(KEY, 3, "active"),
                makeSkill(KEY, 2, "deprecated"),
                makeSkill(KEY, 1, "deprecated"));
        when(repository.findAllByTenantIdAndKey(TENANT, KEY)).thenReturn(versions);

        List<SkillEntity> result = service.getHistory(TENANT, KEY);

        assertEquals(3, result.size());
        assertEquals(3, result.get(0).getVersion());
    }

    // ── Rollback ─────────────────────────────────────────────────

    @Test
    void should_rollback_when_targetVersionExists() {
        SkillEntity v1 = makeSkill(KEY, 1, "deprecated");
        when(repository.findByTenantIdAndKeyAndVersion(TENANT, KEY, 1)).thenReturn(Optional.of(v1));
        when(repository.deprecateByTenantIdAndKey(TENANT, KEY)).thenReturn(1);
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        Optional<SkillEntity> result = service.rollback(TENANT, KEY, 1);

        assertTrue(result.isPresent());
        assertEquals("active", result.get().getStatus());
        verify(repository).deprecateByTenantIdAndKey(TENANT, KEY);
    }

    @Test
    void should_returnEmpty_when_rollbackTargetNotFound() {
        when(repository.findByTenantIdAndKeyAndVersion(TENANT, KEY, 99)).thenReturn(Optional.empty());

        Optional<SkillEntity> result = service.rollback(TENANT, KEY, 99);

        assertTrue(result.isEmpty());
        verify(repository, never()).deprecateByTenantIdAndKey(any(), any());
    }

    // ── Feedback ─────────────────────────────────────────────────

    @Test
    void should_incrementSuccess_when_positiveFeedback() {
        SkillEntity entity = makeSkill(KEY, 1, "active");
        when(repository.findActiveByTenantIdAndKey(TENANT, KEY)).thenReturn(Optional.of(entity));
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        Optional<SkillEntity> result = service.recordFeedback(TENANT, KEY, true);

        assertTrue(result.isPresent());
        assertEquals(1, result.get().getUseCount());
        assertEquals(1, result.get().getSuccessCount());
        assertEquals(0, result.get().getFailureCount());
    }

    @Test
    void should_incrementFailure_when_negativeFeedback() {
        SkillEntity entity = makeSkill(KEY, 1, "active");
        when(repository.findActiveByTenantIdAndKey(TENANT, KEY)).thenReturn(Optional.of(entity));
        when(repository.save(any())).thenAnswer(inv -> inv.getArgument(0));

        Optional<SkillEntity> result = service.recordFeedback(TENANT, KEY, false);

        assertTrue(result.isPresent());
        assertEquals(1, result.get().getUseCount());
        assertEquals(0, result.get().getSuccessCount());
        assertEquals(1, result.get().getFailureCount());
    }

    @Test
    void should_returnEmpty_when_feedbackOnNonexistentSkill() {
        when(repository.findActiveByTenantIdAndKey(TENANT, "gone")).thenReturn(Optional.empty());

        Optional<SkillEntity> result = service.recordFeedback(TENANT, "gone", true);

        assertTrue(result.isEmpty());
    }

    // ── List ─────────────────────────────────────────────────────

    @Test
    void should_listActiveSkills_when_noFilter() {
        List<SkillEntity> skills = List.of(makeSkill("a", 1, "active"), makeSkill("b", 1, "active"));
        when(repository.findActiveByTenantId(TENANT)).thenReturn(skills);

        List<SkillEntity> result = service.listActive(TENANT);

        assertEquals(2, result.size());
    }

    @Test
    void should_filterByCategory_when_categoryProvided() {
        List<SkillEntity> skills = List.of(makeSkill("a", 1, "active"));
        when(repository.findActiveByTenantIdAndCategory(TENANT, "build")).thenReturn(skills);

        List<SkillEntity> result = service.listActiveByCategory(TENANT, "build");

        assertEquals(1, result.size());
    }

    // ── Triggers ─────────────────────────────────────────────────

    @Test
    void should_returnSkillsWithTriggers_when_requested() {
        SkillEntity withTrigger = makeSkill("a", 1, "active");
        withTrigger.setTriggerErrors("[{\"pattern\":\"NPE\"}]");
        when(repository.findActiveWithTriggers(TENANT)).thenReturn(List.of(withTrigger));

        List<SkillEntity> result = service.getActiveWithTriggers(TENANT);

        assertEquals(1, result.size());
        assertNotNull(result.get(0).getTriggerErrors());
    }

    // ── Helpers ──────────────────────────────────────────────────

    private SkillEntity makeSkill(String key, int version, String status) {
        SkillEntity e = new SkillEntity(TENANT, key, version, "Title for " + key,
                "Problem description", "[{\"order\":1,\"action\":\"do something\"}]");
        if (!"active".equals(status)) {
            e.setStatus(status);
        }
        return e;
    }
}
