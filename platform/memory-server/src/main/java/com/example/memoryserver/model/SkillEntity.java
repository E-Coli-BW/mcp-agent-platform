package com.example.memoryserver.model;

import jakarta.persistence.*;
import org.hibernate.annotations.Filter;

import java.time.Instant;
import java.util.HashSet;
import java.util.Set;

/**
 * Skill entity — a versioned, structured, auto-activatable reusable workflow.
 *
 * Differs from MemoryEntity in:
 * - Versioning: each edit creates a new row (version++) instead of overwrite
 * - Structure: problem/steps/pitfalls are first-class fields, not free-text
 * - Activation: trigger_patterns/trigger_errors enable auto-surfacing
 * - Feedback: use_count/success_count/failure_count track effectiveness
 *
 * Multi-tenant security: same Hibernate @Filter pattern as MemoryEntity.
 */
@Entity
@Table(name = "skills",
    uniqueConstraints = @UniqueConstraint(
        name = "uk_tenant_key_version", columnNames = {"tenant_id", "\"key\"", "version"}),
    indexes = {
        @Index(name = "idx_skills_tenant_status", columnList = "tenant_id, status"),
        @Index(name = "idx_skills_tenant_category", columnList = "tenant_id, category"),
    })
@Filter(name = "tenantFilter", condition = "tenant_id = :tenantId")
public class SkillEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    private String id;

    @Column(name = "tenant_id", nullable = false)
    private String tenantId;

    @Column(name = "\"key\"", nullable = false)
    private String key;

    @Column(nullable = false)
    private int version;

    @Column(nullable = false, length = 16)
    private String status; // active | deprecated | draft

    // ── Structured body ──────────────────────────────────────

    @Column(nullable = false)
    private String title;

    @Column(length = 64)
    private String category;

    @Column(columnDefinition = "TEXT", nullable = false)
    private String problem;

    @Column(columnDefinition = "TEXT")
    private String preconditions;

    /** Ordered array of step objects: [{order, action, tool?, verification?}] */
    @Column(columnDefinition = "TEXT", nullable = false)
    private String steps; // stored as JSON string

    @Column(name = "expected_outcome", columnDefinition = "TEXT")
    private String expectedOutcome;

    /** Array of common mistakes: [{description, severity?}] */
    @Column(columnDefinition = "TEXT")
    private String pitfalls; // stored as JSON string

    // ── Activation metadata ──────────────────────────────────

    /** [{type: "regex"|"keyword"|"error_class", pattern?: string, terms?: string[]}] */
    @Column(name = "trigger_patterns", columnDefinition = "TEXT")
    private String triggerPatterns; // stored as JSON string

    /** Array of tool names that hint this skill is relevant */
    @Column(name = "trigger_tools", columnDefinition = "TEXT")
    private String triggerTools; // stored as JSON string

    /** [{pattern: "regex_string"}] — matched against tool error output */
    @Column(name = "trigger_errors", columnDefinition = "TEXT")
    private String triggerErrors; // stored as JSON string

    // ── Relationships ────────────────────────────────────────

    /** Array of skill keys this builds upon */
    @Column(name = "depends_on", columnDefinition = "TEXT")
    private String dependsOn; // stored as JSON string

    @Column(columnDefinition = "TEXT")
    @Convert(converter = StringSetConverter.class)
    private Set<String> tags = new HashSet<>();

    // ── Feedback ─────────────────────────────────────────────

    @Column(name = "use_count")
    private int useCount;

    @Column(name = "success_count")
    private int successCount;

    @Column(name = "failure_count")
    private int failureCount;

    // ── Audit ────────────────────────────────────────────────

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @Column(name = "created_by", length = 128)
    private String createdBy;

    // ── Constructors ─────────────────────────────────────────

    protected SkillEntity() {} // JPA requirement

    public SkillEntity(String tenantId, String key, int version, String title,
                       String problem, String steps) {
        this.tenantId = tenantId;
        this.key = key;
        this.version = version;
        this.status = "active";
        this.title = title;
        this.problem = problem;
        this.steps = steps;
        this.createdAt = Instant.now();
        this.updatedAt = this.createdAt;
    }

    // ── Business Methods ─────────────────────────────────────

    public void deprecate() {
        this.status = "deprecated";
        this.updatedAt = Instant.now();
    }

    public void activate() {
        this.status = "active";
        this.updatedAt = Instant.now();
    }

    public void recordUse(boolean success) {
        this.useCount++;
        if (success) {
            this.successCount++;
        } else {
            this.failureCount++;
        }
        this.updatedAt = Instant.now();
    }

    // ── Getters & Setters ────────────────────────────────────

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getTenantId() { return tenantId; }
    public String getKey() { return key; }
    public int getVersion() { return version; }
    public String getStatus() { return status; }
    public void setStatus(String status) { this.status = status; }
    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }
    public String getCategory() { return category; }
    public void setCategory(String category) { this.category = category; }
    public String getProblem() { return problem; }
    public void setProblem(String problem) { this.problem = problem; }
    public String getPreconditions() { return preconditions; }
    public void setPreconditions(String preconditions) { this.preconditions = preconditions; }
    public String getSteps() { return steps; }
    public void setSteps(String steps) { this.steps = steps; }
    public String getExpectedOutcome() { return expectedOutcome; }
    public void setExpectedOutcome(String expectedOutcome) { this.expectedOutcome = expectedOutcome; }
    public String getPitfalls() { return pitfalls; }
    public void setPitfalls(String pitfalls) { this.pitfalls = pitfalls; }
    public String getTriggerPatterns() { return triggerPatterns; }
    public void setTriggerPatterns(String triggerPatterns) { this.triggerPatterns = triggerPatterns; }
    public String getTriggerTools() { return triggerTools; }
    public void setTriggerTools(String triggerTools) { this.triggerTools = triggerTools; }
    public String getTriggerErrors() { return triggerErrors; }
    public void setTriggerErrors(String triggerErrors) { this.triggerErrors = triggerErrors; }
    public String getDependsOn() { return dependsOn; }
    public void setDependsOn(String dependsOn) { this.dependsOn = dependsOn; }
    public Set<String> getTags() { return tags; }
    public void setTags(Set<String> tags) { this.tags = tags; }
    public int getUseCount() { return useCount; }
    public int getSuccessCount() { return successCount; }
    public int getFailureCount() { return failureCount; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public String getCreatedBy() { return createdBy; }
    public void setCreatedBy(String createdBy) { this.createdBy = createdBy; }

    @Override
    public String toString() {
        return "SkillEntity[id=" + id
                + ", tenant=" + tenantId
                + ", key=" + key
                + ", v=" + version
                + ", status=" + status
                + ", title=" + title + "]";
    }
}
