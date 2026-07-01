package com.example.memoryserver.security;

import com.example.mcp.common.security.TenantContext;
import jakarta.persistence.EntityManager;
import org.hibernate.Session;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.annotation.Before;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * AOP aspect that auto-enables the Hibernate tenant filter before every
 * repository/service method that accesses the database.
 *
 * This is a defense-in-depth layer: even if a developer forgets to pass tenantId
 * in a query, Hibernate will auto-append WHERE tenant_id = :tenantId.
 *
 * Works by intercepting all public methods in the service and repository packages,
 * enabling the filter on the current Hibernate session.
 */
@Aspect
@Component
public class TenantFilterAspect {

    private static final Logger log = LoggerFactory.getLogger(TenantFilterAspect.class);

    private final EntityManager entityManager;

    public TenantFilterAspect(EntityManager entityManager) {
        this.entityManager = entityManager;
    }

    @Before("execution(* com.example.memoryserver.service..*(..)) || " +
            "execution(* com.example.memoryserver.repository..*(..))")
    public void enableTenantFilter() {
        String tenantId = TenantContext.getOrNull();
        if (tenantId != null) {
            Session session = entityManager.unwrap(Session.class);
            var filter = session.enableFilter("tenantFilter");
            filter.setParameter("tenantId", tenantId);
        }
    }
}
