package com.example.auth.model;

import jakarta.persistence.*;

/**
 * Authorization policy — defines what an actor can do on a target service for a tenant.
 *
 * Policy = (actor, audience, tenant, permissions)
 *
 * This model unifies M2M and user auth:
 * - SERVICE actor: agent-server → memory-server for tenant-* with MEMORY_READ,MEMORY_WRITE
 * - USER actor: alice@t1.com → agent-server for tenant-1 with CHAT,MEMORY_READ
 */
@Entity
@Table(name = "auth_policies",
       uniqueConstraints = @UniqueConstraint(columnNames = {"actor", "audience", "tenantId"}))
public class AuthPolicy {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private String actor;        // client_id or user email

    @Column(nullable = false, length = 20)
    @Enumerated(EnumType.STRING)
    private ActorType actorType; // SERVICE or USER

    @Column(nullable = false)
    private String audience;     // target service name or "*"

    @Column(nullable = false, length = 100)
    private String tenantId;     // tenant or "*" (wildcard = all tenants)

    @Column(nullable = false, length = 500)
    private String permissions;  // comma-separated: "MEMORY_READ,MEMORY_WRITE"

    @Column(nullable = false)
    private boolean enabled = true;

    public enum ActorType { SERVICE, USER }

    public AuthPolicy() {}

    public AuthPolicy(String actor, ActorType actorType, String audience,
                      String tenantId, String permissions) {
        this.actor = actor;
        this.actorType = actorType;
        this.audience = audience;
        this.tenantId = tenantId;
        this.permissions = permissions;
    }

    // Getters
    public Long getId() { return id; }
    public String getActor() { return actor; }
    public ActorType getActorType() { return actorType; }
    public String getAudience() { return audience; }
    public String getTenantId() { return tenantId; }
    public String getPermissions() { return permissions; }
    public boolean isEnabled() { return enabled; }

    // Setters
    public void setId(Long id) { this.id = id; }
    public void setActor(String actor) { this.actor = actor; }
    public void setActorType(ActorType actorType) { this.actorType = actorType; }
    public void setAudience(String audience) { this.audience = audience; }
    public void setTenantId(String tenantId) { this.tenantId = tenantId; }
    public void setPermissions(String permissions) { this.permissions = permissions; }
    public void setEnabled(boolean enabled) { this.enabled = enabled; }

    public java.util.List<String> getPermissionList() {
        if (permissions == null || permissions.isBlank()) return java.util.List.of();
        return java.util.Arrays.asList(permissions.split(","));
    }
}
