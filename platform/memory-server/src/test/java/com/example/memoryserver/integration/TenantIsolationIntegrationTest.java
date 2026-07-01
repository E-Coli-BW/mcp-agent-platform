package com.example.memoryserver.integration;

import com.example.mcp.common.security.TenantContext;
import com.example.mcp.common.security.TenantSecurityException;

import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.model.dto.MemoryRequest;
import com.example.mcp.common.security.TenantContext;
import com.example.memoryserver.service.MemoryService;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Integration tests specifically for multi-tenancy security.
 * Runs against real PostgreSQL to verify:
 *
 * 1. Tenant isolation at service level (explicit tenantId param)
 * 2. Hibernate @Filter auto-injects tenant_id on queries
 * 3. Cross-tenant data leak is impossible through normal API
 * 4. TenantContext tampering is blocked
 *
 * Prerequisites: docker compose up -d (postgres on localhost:5432)
 */
@SpringBootTest
@ActiveProfiles("integration-test")
class TenantIsolationIntegrationTest {

    @Autowired
    private MemoryService service;

    @BeforeEach
    void setup() {
        // Clean test data
        TenantContext.clear();
        for (String tid : List.of("tenant-A", "tenant-B", "attacker")) {
            TenantContext.set(tid);
            service.list(tid, null, null).forEach(e -> service.delete(tid, e.getKey()));
            TenantContext.clear();
        }
    }

    @AfterEach
    void cleanup() {
        TenantContext.clear();
    }

    // ── Basic Isolation ──────────────────────────────────────────

    @Test
    void tenants_cannot_see_each_others_data() {
        // Tenant A creates data
        service.set("tenant-A", new MemoryRequest("secret", "A's confidential data", null, null, null));

        // Tenant B creates data with same key
        service.set("tenant-B", new MemoryRequest("secret", "B's confidential data", null, null, null));

        // Each tenant sees only their own
        assertEquals("A's confidential data", service.get("tenant-A", "secret").get().getContent());
        assertEquals("B's confidential data", service.get("tenant-B", "secret").get().getContent());

        // List returns only own entries
        assertEquals(1, service.list("tenant-A", null, null).size());
        assertEquals(1, service.list("tenant-B", null, null).size());

        // Count is tenant-scoped
        String ctxA = service.context("tenant-A");
        assertTrue(ctxA.contains("\"totalMemories\":1") || ctxA.contains("\"totalMemories\": 1"));
    }

    @Test
    void delete_only_affects_own_tenant() {
        service.set("tenant-A", new MemoryRequest("shared-key", "A data", null, null, null));
        service.set("tenant-B", new MemoryRequest("shared-key", "B data", null, null, null));

        // Tenant A deletes their key
        assertTrue(service.delete("tenant-A", "shared-key"));

        // Tenant B's data is unaffected
        assertTrue(service.get("tenant-B", "shared-key").isPresent());
        assertEquals("B data", service.get("tenant-B", "shared-key").get().getContent());

        // Tenant A's data is gone
        assertTrue(service.get("tenant-A", "shared-key").isEmpty());
    }

    @Test
    void search_only_returns_own_tenant_results() {
        service.set("tenant-A", new MemoryRequest("java-guide", "Java Spring Boot tutorial", null, null, null));
        service.set("tenant-B", new MemoryRequest("java-guide", "Java is dangerous - security report", null, null, null));

        // Tenant A searches — should only find their own entry
        var resultsA = service.search("tenant-A", "java", null, null, 10);
        assertEquals(1, resultsA.size());
        assertEquals("Java Spring Boot tutorial", resultsA.get(0).entity().getContent());

        // Tenant B searches — should only find their own entry
        var resultsB = service.search("tenant-B", "java", null, null, 10);
        assertEquals(1, resultsB.size());
        assertTrue(resultsB.get(0).entity().getContent().contains("security report"));
    }

    // ── TenantContext Tampering (Application Layer) ──────────────

    @Test
    void tenantContext_tampering_midRequest_isBlocked() {
        TenantContext.set("tenant-A");

        // Simulate a malicious interceptor trying to escalate to admin tenant
        assertThrows(SecurityException.class,
                () -> TenantContext.set("admin-tenant"));

        // Original tenant is preserved
        assertEquals("tenant-A", TenantContext.get());
    }

    @Test
    void tenantContext_tampering_toAnotherUser_isBlocked() {
        TenantContext.set("tenant-A");

        // Simulate lateral movement — attacker tries to access tenant-B's data
        assertThrows(SecurityException.class,
                () -> TenantContext.set("tenant-B"));

        // Still tenant-A
        assertEquals("tenant-A", TenantContext.get());
    }

    // ── JWT Spoofing Gap (Documented) ────────────────────────────
    // These tests document what IS and ISN'T protected

    @Test
    void jwt_with_forged_tenantId_IS_NOT_BLOCKED_if_signature_is_valid() {
        // This test documents the known gap:
        // If an attacker has the JWT signing key, they can forge tokens for any tenant.
        // The system treats any validly-signed JWT as trusted.
        //
        // MITIGATION (not yet implemented):
        // 1. Use asymmetric keys (RS256) — server only has public key, can't forge
        // 2. Validate against IdP (Keycloak, Auth0) — check token wasn't revoked
        // 3. Add tenant whitelist — only allow known tenant IDs
        //
        // For now, we prove this by showing that the service layer happily accepts
        // any tenantId string — it trusts the caller (JWT filter) to provide a valid one.

        // An "attacker" tenant can create data
        service.set("attacker", new MemoryRequest("evil-key", "evil data", null, null, null));
        Optional<MemoryEntity> result = service.get("attacker", "evil-key");
        assertTrue(result.isPresent());
        // This succeeds because the service layer doesn't validate tenant IDs against an allowlist

        // Cleanup
        service.delete("attacker", "evil-key");
    }
}
