package com.example.mcp.common.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Immutable-per-request tenant context.
 * Shared across all MCP services.
 *
 * Security:
 * - set() can only be called ONCE per request (by JwtAuthFilter)
 * - Subsequent set() with a DIFFERENT tenant → TenantSecurityException
 * - Same-value set() is idempotent (safe for filter retries)
 * - clear() resets for the next request (called in JwtAuthFilter finally block)
 */
public final class TenantContext {
    private static final Logger log = LoggerFactory.getLogger(TenantContext.class);
    private static final ThreadLocal<String> CURRENT_TENANT = new ThreadLocal<>();
    private static final ThreadLocal<Boolean> SEALED = ThreadLocal.withInitial(() -> false);

    private TenantContext() {}

    public static void set(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            throw new TenantSecurityException("Tenant ID cannot be null or blank");
        }
        String existing = CURRENT_TENANT.get();
        if (SEALED.get() && existing != null && !existing.equals(tenantId)) {
            log.error("SECURITY: Tenant tampering detected! Current={}, Attempted={}", existing, tenantId);
            throw new TenantSecurityException(
                    "Tenant context sealed to '" + existing + "', cannot change to '" + tenantId + "'");
        }
        CURRENT_TENANT.set(tenantId);
        SEALED.set(true);
    }

    public static String get() {
        String tid = CURRENT_TENANT.get();
        if (tid == null) throw new IllegalStateException("No tenant context set — is JwtAuthFilter configured?");
        return tid;
    }

    public static String getOrNull() { return CURRENT_TENANT.get(); }

    public static void clear() {
        CURRENT_TENANT.remove();
        SEALED.remove();
    }
}
