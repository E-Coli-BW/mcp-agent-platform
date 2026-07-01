package com.example.memoryserver.audit;

import com.example.mcp.common.security.AgentLineageContext;
import com.example.mcp.common.security.AgentLineageContext.AgentLineage;
import com.example.mcp.common.security.TenantContext;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;

/**
 * AOP aspect that logs all MCP tool invocations with:
 * who (user), what (tool name), when, duration, and outcome.
 *
 * <p>Audit log is written to a dedicated logger "AUDIT" for
 * separate log file / ELK pipeline routing.
 *
 * <p>Each line also carries the {@code root_session}, {@code parent_session},
 * and {@code depth} of the calling agent fleet when the caller is the
 * Python {@code agent-server} (it injects {@code X-Root-Session-Id},
 * {@code X-Parent-Session-Id}, {@code X-Agent-Depth} headers). For direct
 * curl traffic or other clients these fields are {@code "-"}/{@code 0} —
 * keeping the line shape constant so log parsers never have to special-case.
 *
 * <p>The lineage fields let the dashboard reconstruct an entire spawn tree
 * by grouping audit rows on {@code root_session} and drawing parent→child
 * edges via {@code parent_session}.
 */
@Aspect
@Component
public class AuditAspect {

    private static final Logger audit = LoggerFactory.getLogger("AUDIT");

    /**
     * Intercept all @Tool methods in the tool package.
     */
    @Around("execution(* com.example.memoryserver.tool..*(..))")
    public Object auditToolCall(ProceedingJoinPoint pjp) throws Throwable {
        String method = pjp.getSignature().getName();
        String user = getUser();
        String tenant = getTenant();
        AgentLineage lineage = getLineage();
        Instant start = Instant.now();

        try {
            Object result = pjp.proceed();
            long ms = Duration.between(start, Instant.now()).toMillis();
            // Field order is locked — the codeexec-server AuditAspect emits
            // the same order so a single log-parser regex handles both.
            audit.info("tenant={} user={} tool={} root_session={} parent_session={} depth={} duration={}ms status=OK",
                    tenant, user, method,
                    lineage.rootSessionId(), lineage.parentSessionId(), lineage.depth(),
                    ms);
            return result;
        } catch (Exception e) {
            long ms = Duration.between(start, Instant.now()).toMillis();
            audit.warn("tenant={} user={} tool={} root_session={} parent_session={} depth={} duration={}ms status=FAIL error={}",
                    tenant, user, method,
                    lineage.rootSessionId(), lineage.parentSessionId(), lineage.depth(),
                    ms, e.getMessage());
            throw e;
        }
    }

    private String getUser() {
        try {
            var auth = SecurityContextHolder.getContext().getAuthentication();
            return auth != null ? auth.getName() : "anonymous";
        } catch (Exception e) {
            return "unknown";
        }
    }

    private String getTenant() {
        try {
            return TenantContext.get();
        } catch (Exception e) {
            return "unknown";
        }
    }

    /**
     * Read the agent fleet lineage headers from the current HTTP request.
     * Never throws — observability must not break the request — falls back
     * to {@link AgentLineageContext#ABSENT} on any unexpected failure
     * (e.g. accessed from a non-web thread).
     */
    private AgentLineage getLineage() {
        try {
            return AgentLineageContext.fromCurrentRequest();
        } catch (Exception e) {
            return AgentLineageContext.ABSENT;
        }
    }
}
