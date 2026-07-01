package com.example.memoryserver.model;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import jakarta.persistence.*;
import org.hibernate.annotations.Filter;
import org.hibernate.annotations.FilterDef;
import org.hibernate.annotations.ParamDef;
import java.time.Instant;
import java.util.HashSet;
import java.util.Set;

/**
 * Memory entry entity — dual-annotated for JPA and MyBatis-Plus.
 *
 * Multi-tenant security:
 * - Hibernate @Filter auto-injects WHERE tenant_id = :tenantId on every query
 * - Filter must be enabled per-session via TenantFilterAspect (AOP)
 * - This is a defense-in-depth layer — service code also passes tenantId explicitly
 */
@Entity
@Table(name = "memories",
    uniqueConstraints = @UniqueConstraint(
        name = "uk_tenant_key", columnNames = {"tenant_id", "\"key\""}),
    indexes = {
        @Index(name = "idx_tenant_ns", columnList = "tenant_id, namespace"),
        @Index(name = "idx_tenant_updated", columnList = "tenant_id, updated_at DESC")
    })
@FilterDef(name = "tenantFilter", parameters = @ParamDef(name = "tenantId", type = String.class))
@Filter(name = "tenantFilter", condition = "tenant_id = :tenantId")
@TableName("memories")
public class MemoryEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @TableId(type = IdType.ASSIGN_UUID)
    private String id;

    @Column(name = "tenant_id", nullable = false)
    private String tenantId;

    @Column(name = "\"key\"", nullable = false)
    private String key;

    @Column(columnDefinition = "TEXT", nullable = false)
    private String content;

    @Column(nullable = false)
    private String namespace;

    /** Stored as JSONB in PostgreSQL, TEXT in H2. */
    @Column(columnDefinition = "TEXT")
    @Convert(converter = StringSetConverter.class)
    @TableField(typeHandler = com.example.memoryserver.model.TagsTypeHandler.class)
    private Set<String> tags = new HashSet<>();

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @Column(name = "last_accessed_at")
    private Instant lastAccessedAt;

    @Column(name = "access_count")
    private int accessCount;

    private boolean pinned;

    /** Optimistic locking — prevents concurrent write conflicts. */
    @Version
    private Long version;

    // ── Constructors ─────────────────────────────────────────────

    protected MemoryEntity() {} // JPA requirement

    public MemoryEntity(String tenantId, String key, String content, String namespace) {
        this.tenantId = tenantId;
        this.key = key;
        this.content = content;
        this.namespace = namespace;
        this.createdAt = Instant.now();
        this.updatedAt = this.createdAt;
    }

    // ── Business Methods ─────────────────────────────────────────

    /** Record an access — increments count and updates timestamp. */
    public void recordAccess() {
        this.accessCount++;
        this.lastAccessedAt = Instant.now();
    }

    /** Update content and metadata. */
    public void updateContent(String content, String namespace, Set<String> tags, Boolean pinned) {
        this.content = content;
        if (namespace != null && !namespace.isEmpty()) this.namespace = namespace;
        if (tags != null) this.tags = tags;
        if (pinned != null) this.pinned = pinned;
        this.updatedAt = Instant.now();
    }

    // ── Getters ──────────────────────────────────────────────────

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getTenantId() { return tenantId; }
    public String getKey() { return key; }
    public String getContent() { return content; }
    public String getNamespace() { return namespace; }
    public Set<String> getTags() { return tags; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public Instant getLastAccessedAt() { return lastAccessedAt; }
    public int getAccessCount() { return accessCount; }
    public boolean isPinned() { return pinned; }
    public Long getVersion() { return version; }

    /**
     * Alibaba OOP #12: POJO classes must implement a toString method.
     * Truncates content to 100 chars for log readability.
     */
    @Override
    public String toString() {
        String truncated = content != null && content.length() > 100
                ? content.substring(0, 100) + "..." : content;
        return "MemoryEntity[id=" + id
                + ", tenant=" + tenantId
                + ", key=" + key
                + ", namespace=" + namespace
                + ", content=" + truncated
                + ", pinned=" + pinned
                + ", version=" + version + "]";
    }
}
