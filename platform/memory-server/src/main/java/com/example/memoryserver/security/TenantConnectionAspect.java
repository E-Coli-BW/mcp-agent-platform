package com.example.memoryserver.security;

import com.example.mcp.common.security.TenantContext;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Sets PostgreSQL session variable `app.tenant_id` on every repository call.
 * This enables Row-Level Security (RLS) at the database level.
 *
 * IMPORTANT: Uses JdbcTemplate which participates in Spring's transaction-managed
 * connection. This ensures the SET and subsequent queries run on the SAME connection.
 *
 * Previous bug: used dataSource.getConnection() directly, which returned a new
 * connection that was immediately closed — the actual query got a different connection
 * from the pool, so RLS was never enforced.
 *
 * Only active when using PostgreSQL (not H2).
 * Activated by property: memory.security.rls.enabled=true
 */
@Aspect
@Component
@ConditionalOnProperty(name = "memory.security.rls.enabled", havingValue = "true")
public class TenantConnectionAspect {

    private static final Logger log = LoggerFactory.getLogger(TenantConnectionAspect.class);

    private final JdbcTemplate jdbcTemplate;

    public TenantConnectionAspect(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Before any repository method, set the tenant_id on the current
     * transaction-bound connection using PostgreSQL's set_config() function,
     * which supports parameterized queries (no SQL injection risk).
     *
     * set_config(name, value, is_local) — is_local=true means the setting
     * is scoped to the current transaction only (equivalent to SET LOCAL).
     */
    @Before("execution(* com.example.memoryserver.repository..*(..))")
    public void setTenantOnConnection() {
        String tenantId = TenantContext.getOrNull();
        if (tenantId == null) return;

        try {
            jdbcTemplate.update("SELECT set_config('app.tenant_id', ?, true)", tenantId);
        } catch (Exception e) {
            log.warn("Failed to set app.tenant_id on connection: {}", e.getMessage());
        }
    }
}
