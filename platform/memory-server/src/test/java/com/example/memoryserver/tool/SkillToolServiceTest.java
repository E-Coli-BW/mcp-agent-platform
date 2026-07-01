package com.example.memoryserver.tool;

import com.example.memoryserver.model.SkillEntity;
import com.example.memoryserver.service.SkillService;
import com.example.mcp.common.security.TenantContext;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
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
 * Unit tests for SkillToolService.
 * Validates MCP tool contract: always returns String, never throws,
 * uses emoji prefixes, handles errors gracefully.
 */
@ExtendWith(MockitoExtension.class)
class SkillToolServiceTest {

    @Mock private SkillService skillService;

    private SkillToolService toolService;
    private final ObjectMapper mapper = new ObjectMapper();

    private static final String TENANT = "test-tenant";
    private static final String KEY = "maven-stale-jar-fix";

    @BeforeEach
    void setUp() {
        toolService = new SkillToolService(skillService, mapper);
        TenantContext.set(TENANT);
    }

    @AfterEach
    void tearDown() {
        TenantContext.clear();
    }

    // ── skill_set ────────────────────────────────────────────────

    @Test
    void should_returnSuccess_when_skillSetSucceeds() {
        SkillEntity saved = makeSkill(KEY, 1);
        when(skillService.set(eq(TENANT), eq(KEY), any(), any(), any(), any(),
                any(), any(), any(), any(), any(), any(), any(), any(), any()))
                .thenReturn(saved);

        String result = toolService.skill_set(KEY, "Fix stale JAR",
                "ClassNotFoundException after edits",
                "[{\"order\":1,\"action\":\"run mvn install\"}]",
                "build", null, null, null, List.of("build"));

        assertTrue(result.startsWith("✅"));
        assertTrue(result.contains(KEY));
        assertTrue(result.contains("v1"));
    }

    @Test
    void should_returnError_when_skillSetFails() {
        when(skillService.set(eq(TENANT), eq(KEY), any(), any(), any(), any(),
                any(), any(), any(), any(), any(), any(), any(), any(), any()))
                .thenThrow(new RuntimeException("DB connection failed"));

        String result = toolService.skill_set(KEY, "Title", "Problem", "[]",
                null, null, null, null, null);

        assertTrue(result.startsWith("❌"));
        assertTrue(result.contains("DB connection failed"));
    }

    // ── skill_get ────────────────────────────────────────────────

    @Test
    void should_renderBody_when_skillExists() {
        SkillEntity entity = makeSkill(KEY, 2);
        entity.setCategory("build");
        when(skillService.getActive(TENANT, KEY)).thenReturn(Optional.of(entity));

        String result = toolService.skill_get(KEY, null);

        assertTrue(result.contains("Fix stale JAR"));
        assertTrue(result.contains("v2"));
        assertTrue(result.contains("Steps"));
    }

    @Test
    void should_returnNotFound_when_skillMissing() {
        when(skillService.getActive(TENANT, "nope")).thenReturn(Optional.empty());

        String result = toolService.skill_get("nope", null);

        assertTrue(result.startsWith("❌"));
        assertTrue(result.contains("not found"));
    }

    @Test
    void should_getSpecificVersion_when_versionProvided() {
        SkillEntity v1 = makeSkill(KEY, 1);
        when(skillService.getVersion(TENANT, KEY, 1)).thenReturn(Optional.of(v1));

        String result = toolService.skill_get(KEY, 1);

        assertTrue(result.contains("v1"));
    }

    // ── skill_list ───────────────────────────────────────────────

    @Test
    void should_listSkills_when_available() {
        List<SkillEntity> skills = List.of(makeSkill("a", 1), makeSkill("b", 2));
        when(skillService.listActive(TENANT)).thenReturn(skills);

        String result = toolService.skill_list(null, null);

        assertTrue(result.startsWith("📋"));
        assertTrue(result.contains("2 skill(s)"));
        assertTrue(result.contains("`a`"));
        assertTrue(result.contains("`b`"));
    }

    @Test
    void should_returnEmpty_when_noSkills() {
        when(skillService.listActive(TENANT)).thenReturn(List.of());

        String result = toolService.skill_list(null, null);

        assertTrue(result.contains("No skills found"));
    }

    @Test
    void should_filterByCategory_when_categoryProvided() {
        List<SkillEntity> skills = List.of(makeSkill("a", 1));
        when(skillService.listActiveByCategory(TENANT, "build")).thenReturn(skills);

        String result = toolService.skill_list("build", null);

        assertTrue(result.contains("`a`"));
    }

    @Test
    void should_filterByTags_when_tagsProvided() {
        SkillEntity match = makeSkill("a", 1);
        match.setTags(Set.of("maven", "build"));
        SkillEntity noMatch = makeSkill("b", 1);
        noMatch.setTags(Set.of("python"));
        when(skillService.listActive(TENANT)).thenReturn(List.of(match, noMatch));

        String result = toolService.skill_list(null, List.of("maven"));

        assertTrue(result.contains("`a`"));
        assertFalse(result.contains("`b`"));
    }

    // ── skill_history ────────────────────────────────────────────

    @Test
    void should_showHistory_when_versionsExist() {
        List<SkillEntity> history = List.of(
                makeSkill(KEY, 3), makeSkill(KEY, 2), makeSkill(KEY, 1));
        history.get(1).setStatus("deprecated");
        history.get(2).setStatus("deprecated");
        when(skillService.getHistory(TENANT, KEY)).thenReturn(history);

        String result = toolService.skill_history(KEY);

        assertTrue(result.contains("3 versions"));
        assertTrue(result.contains("v3"));
        assertTrue(result.contains("v1"));
    }

    @Test
    void should_returnNotFound_when_noHistory() {
        when(skillService.getHistory(TENANT, KEY)).thenReturn(List.of());

        String result = toolService.skill_history(KEY);

        assertTrue(result.startsWith("❌"));
    }

    // ── skill_rollback ───────────────────────────────────────────

    @Test
    void should_rollback_when_versionExists() {
        SkillEntity v1 = makeSkill(KEY, 1);
        when(skillService.rollback(TENANT, KEY, 1)).thenReturn(Optional.of(v1));

        String result = toolService.skill_rollback(KEY, 1);

        assertTrue(result.contains("🔄"));
        assertTrue(result.contains("v1"));
    }

    @Test
    void should_returnError_when_rollbackTargetMissing() {
        when(skillService.rollback(TENANT, KEY, 99)).thenReturn(Optional.empty());

        String result = toolService.skill_rollback(KEY, 99);

        assertTrue(result.startsWith("❌"));
        assertTrue(result.contains("99"));
    }

    // ── skill_feedback ───────────────────────────────────────────

    @Test
    void should_recordSuccess_when_positiveFeedback() {
        SkillEntity entity = makeSkill(KEY, 1);
        entity.recordUse(true);
        when(skillService.recordFeedback(TENANT, KEY, true)).thenReturn(Optional.of(entity));

        String result = toolService.skill_feedback(KEY, true);

        assertTrue(result.contains("👍"));
        assertTrue(result.contains("1 successes"));
    }

    @Test
    void should_recordFailure_when_negativeFeedback() {
        SkillEntity entity = makeSkill(KEY, 1);
        entity.recordUse(false);
        when(skillService.recordFeedback(TENANT, KEY, false)).thenReturn(Optional.of(entity));

        String result = toolService.skill_feedback(KEY, false);

        assertTrue(result.contains("👎"));
        assertTrue(result.contains("1 failures"));
    }

    @Test
    void should_returnError_when_feedbackOnMissingSkill() {
        when(skillService.recordFeedback(TENANT, "gone", true)).thenReturn(Optional.empty());

        String result = toolService.skill_feedback("gone", true);

        assertTrue(result.startsWith("❌"));
    }

    // ── skill_triggers ───────────────────────────────────────────

    @Test
    void should_returnTriggerJson_when_triggersExist() {
        SkillEntity entity = makeSkill(KEY, 1);
        entity.setTriggerErrors("[{\"pattern\":\"ClassNotFoundException\"}]");
        entity.setTriggerTools("[\"code_shell\"]");
        when(skillService.getActiveWithTriggers(TENANT)).thenReturn(List.of(entity));

        String result = toolService.skill_triggers();

        assertTrue(result.contains("ClassNotFoundException"));
        assertTrue(result.contains("code_shell"));
        assertTrue(result.contains(KEY));
    }

    @Test
    void should_returnEmptyArray_when_noTriggers() {
        when(skillService.getActiveWithTriggers(TENANT)).thenReturn(List.of());

        String result = toolService.skill_triggers();

        assertEquals("[]", result);
    }

    // ── Helpers ──────────────────────────────────────────────────

    private SkillEntity makeSkill(String key, int version) {
        return new SkillEntity(TENANT, key, version, "Fix stale JAR",
                "ClassNotFoundException after mcp-common edits",
                "[{\"order\":1,\"action\":\"run mvn install\",\"verification\":\"BUILD SUCCESS\"}]");
    }
}
