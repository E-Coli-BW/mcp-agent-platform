package com.example.memoryserver.tool;

import com.example.memoryserver.model.SkillEntity;
import com.example.memoryserver.service.SkillService;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import com.example.mcp.common.security.TenantContext;

import java.util.*;
import java.util.stream.Collectors;

/**
 * MCP tool methods for skills — matches the tool naming convention.
 * All methods return String (MCP contract: never throw, handle errors internally).
 */
@Service
public class SkillToolService {

    private static final Logger log = LoggerFactory.getLogger(SkillToolService.class);

    private final SkillService skillService;
    private final ObjectMapper mapper;

    public SkillToolService(SkillService skillService, ObjectMapper mapper) {
        this.skillService = skillService;
        this.mapper = mapper;
    }

    // ── skill_set ────────────────────────────────────────────────

    @SuppressWarnings("unchecked")
    public String skill_set(String key, String title, String problem, Object steps,
                            String category, String triggerPatterns, String triggerTools,
                            String triggerErrors, List<String> tags) {
        String tenantId = TenantContext.get();
        try {
            String stepsJson = steps instanceof String ? (String) steps : mapper.writeValueAsString(steps);
            Set<String> tagSet = tags != null ? new HashSet<>(tags) : Set.of();

            SkillEntity saved = skillService.set(
                    tenantId, key, title, problem, stepsJson,
                    category, null, null, null,
                    triggerPatterns, triggerTools, triggerErrors,
                    null, tagSet, "agent");

            return "✅ Skill saved: `" + key + "` (v" + saved.getVersion() + ")";
        } catch (Exception e) {
            log.error("skill_set failed: tenant={}, key={}", tenantId, key, e);
            return "❌ Failed to save skill: " + e.getMessage();
        }
    }

    // ── skill_get ────────────────────────────────────────────────

    public String skill_get(String key, Integer version) {
        String tenantId = TenantContext.get();
        try {
            Optional<SkillEntity> opt = version != null
                    ? skillService.getVersion(tenantId, key, version)
                    : skillService.getActive(tenantId, key);

            if (opt.isEmpty()) {
                return "❌ Skill not found: `" + key + "`"
                        + (version != null ? " (v" + version + ")" : "");
            }

            SkillEntity s = opt.get();
            return renderSkillBody(s);
        } catch (Exception e) {
            log.error("skill_get failed: tenant={}, key={}", tenantId, key, e);
            return "❌ Failed to get skill: " + e.getMessage();
        }
    }

    // ── skill_list ───────────────────────────────────────────────

    public String skill_list(String category, List<String> tags) {
        String tenantId = TenantContext.get();
        try {
            List<SkillEntity> skills = category != null
                    ? skillService.listActiveByCategory(tenantId, category)
                    : skillService.listActive(tenantId);

            // Filter by tags if provided
            if (tags != null && !tags.isEmpty()) {
                Set<String> filterTags = new HashSet<>(tags);
                skills = skills.stream()
                        .filter(s -> !Collections.disjoint(s.getTags(), filterTags))
                        .collect(Collectors.toList());
            }

            if (skills.isEmpty()) {
                return "📋 No skills found" + (category != null ? " in category `" + category + "`" : "") + ".";
            }

            StringBuilder sb = new StringBuilder();
            sb.append("📋 ").append(skills.size()).append(" skill(s):\n");
            for (SkillEntity s : skills) {
                sb.append("- `").append(s.getKey()).append("` v").append(s.getVersion());
                sb.append(" — ").append(truncate(s.getProblem(), 80));
                if (s.getCategory() != null) {
                    sb.append(" [").append(s.getCategory()).append("]");
                }
                sb.append("\n");
            }
            return sb.toString().trim();
        } catch (Exception e) {
            log.error("skill_list failed: tenant={}", tenantId, e);
            return "❌ Failed to list skills: " + e.getMessage();
        }
    }

    // ── skill_history ────────────────────────────────────────────

    public String skill_history(String key) {
        String tenantId = TenantContext.get();
        try {
            List<SkillEntity> versions = skillService.getHistory(tenantId, key);
            if (versions.isEmpty()) {
                return "❌ No skill found with key `" + key + "`.";
            }

            StringBuilder sb = new StringBuilder();
            sb.append("📋 History for `").append(key).append("` (").append(versions.size()).append(" versions):\n");
            for (SkillEntity s : versions) {
                sb.append("- v").append(s.getVersion())
                        .append(" [").append(s.getStatus()).append("]")
                        .append(" — ").append(s.getUpdatedAt())
                        .append("\n");
            }
            return sb.toString().trim();
        } catch (Exception e) {
            log.error("skill_history failed: tenant={}, key={}", tenantId, key, e);
            return "❌ Failed to get skill history: " + e.getMessage();
        }
    }

    // ── skill_rollback ───────────────────────────────────────────

    public String skill_rollback(String key, int version) {
        String tenantId = TenantContext.get();
        try {
            Optional<SkillEntity> result = skillService.rollback(tenantId, key, version);
            if (result.isEmpty()) {
                return "❌ Version " + version + " not found for skill `" + key + "`.";
            }
            return "🔄 Rolled back `" + key + "` to v" + version + ".";
        } catch (Exception e) {
            log.error("skill_rollback failed: tenant={}, key={}, version={}", tenantId, key, version, e);
            return "❌ Failed to rollback: " + e.getMessage();
        }
    }

    // ── skill_feedback ───────────────────────────────────────────

    public String skill_feedback(String key, boolean success) {
        String tenantId = TenantContext.get();
        try {
            Optional<SkillEntity> result = skillService.recordFeedback(tenantId, key, success);
            if (result.isEmpty()) {
                return "❌ No active skill found: `" + key + "`.";
            }
            SkillEntity s = result.get();
            String emoji = success ? "👍" : "👎";
            return emoji + " Feedback recorded for `" + key + "` — "
                    + s.getSuccessCount() + " successes, " + s.getFailureCount() + " failures.";
        } catch (Exception e) {
            log.error("skill_feedback failed: tenant={}, key={}", tenantId, key, e);
            return "❌ Failed to record feedback: " + e.getMessage();
        }
    }

    // ── skill_triggers (for agent-server cache loading) ──────────

    public String skill_triggers() {
        String tenantId = TenantContext.get();
        try {
            List<SkillEntity> skills = skillService.getActiveWithTriggers(tenantId);
            List<Map<String, Object>> result = new ArrayList<>();
            for (SkillEntity s : skills) {
                Map<String, Object> entry = new LinkedHashMap<>();
                entry.put("key", s.getKey());
                entry.put("problem", truncate(s.getProblem(), 100));
                if (s.getTriggerPatterns() != null) entry.put("trigger_patterns", s.getTriggerPatterns());
                if (s.getTriggerErrors() != null) entry.put("trigger_errors", s.getTriggerErrors());
                if (s.getTriggerTools() != null) entry.put("trigger_tools", s.getTriggerTools());
                result.add(entry);
            }
            return mapper.writeValueAsString(result);
        } catch (Exception e) {
            log.error("skill_triggers failed: tenant={}", tenantId, e);
            return "[]";
        }
    }

    // ── Helpers ──────────────────────────────────────────────────

    private String renderSkillBody(SkillEntity s) {
        StringBuilder sb = new StringBuilder();
        sb.append("# ").append(s.getTitle()).append(" (v").append(s.getVersion()).append(")\n\n");
        sb.append("**Problem**: ").append(s.getProblem()).append("\n\n");
        if (s.getPreconditions() != null) {
            sb.append("**Preconditions**: ").append(s.getPreconditions()).append("\n\n");
        }
        sb.append("**Steps**:\n").append(formatSteps(s.getSteps())).append("\n");
        if (s.getExpectedOutcome() != null) {
            sb.append("**Expected outcome**: ").append(s.getExpectedOutcome()).append("\n\n");
        }
        if (s.getPitfalls() != null) {
            sb.append("**Pitfalls**: ").append(s.getPitfalls()).append("\n\n");
        }
        sb.append("---\n");
        sb.append("_key=").append(s.getKey())
                .append(", category=").append(s.getCategory())
                .append(", uses=").append(s.getUseCount())
                .append(", success_rate=").append(successRate(s)).append("_");
        return sb.toString();
    }

    private String formatSteps(String stepsJson) {
        try {
            List<?> steps = mapper.readValue(stepsJson, List.class);
            StringBuilder sb = new StringBuilder();
            for (Object step : steps) {
                if (step instanceof Map<?, ?> m) {
                    sb.append(m.get("order")).append(". ").append(m.get("action"));
                    if (m.get("verification") != null) {
                        sb.append(" [verify: ").append(m.get("verification")).append("]");
                    }
                    sb.append("\n");
                }
            }
            return sb.toString();
        } catch (JsonProcessingException e) {
            return stepsJson; // fallback: raw JSON
        }
    }

    private String successRate(SkillEntity s) {
        if (s.getUseCount() == 0) return "N/A";
        int rate = (int) ((s.getSuccessCount() * 100.0) / s.getUseCount());
        return rate + "%";
    }

    private String truncate(String text, int maxLen) {
        if (text == null) return "";
        String oneLine = text.replace("\n", " ").trim();
        return oneLine.length() > maxLen ? oneLine.substring(0, maxLen - 1) + "…" : oneLine;
    }
}
