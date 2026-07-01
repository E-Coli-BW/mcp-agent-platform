package com.example.memoryserver.security;

import com.example.mcp.common.security.TenantContext;
import com.example.mcp.common.security.TenantSecurityException;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for TenantContext tamper-proof behavior.
 * No Spring context needed — pure Java logic.
 */
class TenantContextTest {

    @AfterEach
    void cleanup() {
        TenantContext.clear();
    }

    // ── Normal usage ─────────────────────────────────────────────

    @Test
    void set_and_get_works() {
        TenantContext.set("tenant-A");
        assertEquals("tenant-A", TenantContext.get());
    }

    @Test
    void getOrNull_returnsNull_whenNotSet() {
        assertNull(TenantContext.getOrNull());
    }

    @Test
    void get_throws_whenNotSet() {
        assertThrows(IllegalStateException.class, TenantContext::get);
    }

    @Test
    void clear_resetsContext() {
        TenantContext.set("tenant-A");
        TenantContext.clear();
        assertNull(TenantContext.getOrNull());
    }

    // ── Idempotent set (same value) ──────────────────────────────

    @Test
    void set_sameValue_isIdempotent() {
        TenantContext.set("tenant-A");
        TenantContext.set("tenant-A"); // same value — should not throw
        assertEquals("tenant-A", TenantContext.get());
    }

    // ── ATTACK: Tenant tampering mid-request ─────────────────────

    @Test
    void set_differentValue_afterSealed_throwsTenantSecurityException() {
        TenantContext.set("tenant-A");

        // Simulate malicious interceptor trying to change tenant
        TenantSecurityException ex = assertThrows(TenantSecurityException.class,
                () -> TenantContext.set("tenant-B"));

        assertTrue(ex.getMessage().contains("tenant-A"));
        assertTrue(ex.getMessage().contains("tenant-B"));
        // Original tenant is preserved
        assertEquals("tenant-A", TenantContext.get());
    }

    @Test
    void set_null_throws() {
        assertThrows(TenantSecurityException.class, () -> TenantContext.set(null));
    }

    @Test
    void set_blank_throws() {
        assertThrows(TenantSecurityException.class, () -> TenantContext.set("  "));
    }

    // ── Re-use after clear (next request lifecycle) ──────────────

    @Test
    void clear_thenSet_newTenant_works() {
        TenantContext.set("tenant-A");
        TenantContext.clear(); // end of request 1

        // New request with different tenant — should work
        TenantContext.set("tenant-B");
        assertEquals("tenant-B", TenantContext.get());
    }

    @Test
    void clear_thenSet_thenTamper_blocked() {
        TenantContext.set("tenant-A");
        TenantContext.clear();

        TenantContext.set("tenant-B"); // new request
        // Tampering within new request still blocked
        assertThrows(TenantSecurityException.class,
                () -> TenantContext.set("tenant-C"));
        assertEquals("tenant-B", TenantContext.get());
    }
}
