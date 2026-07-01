package com.example.codeexec.audit;

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
 * AOP aspect that logs all MCP tool invocations on this server with:
 * who (user), what (tool name), when, duration, and outcome.
 *
 * <p>Mirror of {@code memory-server}'s {@code AuditAspect} — the AUDIT
 * line format is intentionally identical so a downstream consumer
 * (e.g. the local dev dashboard at {@code scripts/dev/dashboard/}) can
 * scrape both with the same regex:
 *
 * <pre>AUDIT : tenant=&lt;t&gt; user=&lt;u&gt; tool=&lt;name&gt; root_session=&lt;s&gt; parent_session=&lt;p&gt; depth=&lt;d&gt; duration=&lt;n&gt;ms status=&lt;OK|FAIL&gt;</pre>
 *
 * <p>The {@code root_session}, {@code parent_session}, and {@code depth}
 * fields carry the agent fleet lineage injected by the Python
 * {@code agent-server} as {@code X-Root-Session-Id}, {@code X-Parent-Session-Id},
 * and {@code X-Agent-Depth} request headers. For direct curl traffic these
 * are {@code "-"}/{@code 0} — log shape stays constant either way.
 *
 * <p>Pointcut targets every public method in
 * {@code com.example.codeexec.tool..} so adding a new {@code @Tool}
 * service in that package automatically gets audited — no opt-in
 * needed (the right default for security/observability primitives).
 *
 * <p>Audit lines go to a dedicated SLF4J logger named {@code AUDIT} so
 * that an external log appender can route them to a separate file or
 * to ELK without filtering the main application log.
 */
@Aspect
@Component
public class AuditAspect {

    private static final Logger audit = LoggerFactory.getLogger("AUDIT");

    @Around("execution(* com.example.codeexec.tool..*(..))")
    public Object auditToolCall(ProceedingJoinPoint pjp) throws Throwable {
        String method = pjp.getSignature().getName();
        String user = getUser();
        String tenant = getTenant();
        AgentLineage lineage = getLineage();
        Instant start = Instant.now();

        try {
            Object result = pjp.proceed();
            long ms = Duration.between(start, Instant.now()).toMillis();
            // Field order matches memory-server.AuditAspect exactly — one
            // log-parser regex covers both servers.
            audit.info("tenant={} user={} tool={} root_session={} parent_session={} depth={} duration={}ms status=OK",
                    tenant, user, method,
                    lineage.rootSessionId(), lineage.parentSessionId(), lineage.depth(),
                    ms);
            return result;
        } catch (Exception e) {
            long ms = Duration.between(start, Instant.now()).toMillis();
            // .warn (not .error) — a tool throwing is a normal failure mode
            // (e.g. user sandbox violation) and should not pollute the
            // ERROR channel that operators page on.
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
