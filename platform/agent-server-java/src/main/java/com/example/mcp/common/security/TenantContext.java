package com.example.mcp.common.security;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Immutable-per-request tenant context for WebFlux reactive stack.
 *
 * <p>Mirrors the behavior of mcp-common's TenantContext (Servlet-based) but works
 * with WebFlux. Uses ThreadLocal because WebFlux's Reactor runs request handlers
 * on Netty event loop threads — each request is bound to a thread for the duration
 * of synchronous sections.</p>
 *
 * <p><b>Why not use mcp-common directly?</b> mcp-common's JwtAuthFilter extends
 * Servlet's OncePerRequestFilter. This module uses WebFlux (reactive), which requires
 * WebFilter. The TenantContext API is identical — same package, same class name —
 * so service code is portable between the two.</p>
 *
 * <p>Security: set() can only be called ONCE per request. Subsequent set() with a
 * DIFFERENT tenant throws IllegalStateException. clear() resets for the next request.</p>
 */
public final class TenantContext {

    private static final Logger log = LoggerFactory.getLogger(TenantContext.class);
    private static final ThreadLocal<String> CURRENT_TENANT = new ThreadLocal<>();
    private static final ThreadLocal<Boolean> SEALED = ThreadLocal.withInitial(() -> false);

    private TenantContext() {
    }

    /**
     * Set the tenant ID for the current request. Can only be set once (sealed).
     *
     * @param tenantId the tenant identifier
     * @throws IllegalStateException if a different tenant ID is already set
     */
    public static void set(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            throw new IllegalArgumentException("Tenant ID cannot be null or blank");
        }
        String existing = CURRENT_TENANT.get();
        if (SEALED.get() && existing != null && !existing.equals(tenantId)) {
            log.error("SECURITY: Tenant tampering detected! Current={}, Attempted={}", existing, tenantId);
            throw new IllegalStateException(
                    "Tenant context sealed to '" + existing + "', cannot change to '" + tenantId + "'");
        }
        CURRENT_TENANT.set(tenantId);
        SEALED.set(true);
    }

    /**
     * Get the current tenant ID.
     *
     * @return tenant ID
     * @throws IllegalStateException if no tenant context is set
     */
    public static String get() {
        String tid = CURRENT_TENANT.get();
        if (tid == null) {
            throw new IllegalStateException("No tenant context set — is JWT auth filter configured?");
        }
        return tid;
    }

    /**
     * Get the current tenant ID, or null if not set.
     */
    public static String getOrNull() {
        return CURRENT_TENANT.get();
    }

    /**
     * Clear the tenant context. Called at the end of each request.
     */
    public static void clear() {
        CURRENT_TENANT.remove();
        SEALED.remove();
    }
}
