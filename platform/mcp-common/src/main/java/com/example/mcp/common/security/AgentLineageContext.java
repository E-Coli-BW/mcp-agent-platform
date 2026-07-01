package com.example.mcp.common.security;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

/**
 * Per-request lineage of the calling agent fleet.
 *
 * <p>When an agent (Python {@code agent-server}) calls into a Java backend,
 * it injects three headers describing where this call sits in the fleet:
 *
 * <ul>
 *   <li>{@code X-Root-Session-Id} — the original user chat session_id,
 *       constant across an entire spawn tree</li>
 *   <li>{@code X-Parent-Session-Id} — the IMMEDIATE parent's session_id
 *       (equal to root at depth=0)</li>
 *   <li>{@code X-Agent-Depth} — recursion depth (0 = root, 1 = first child)</li>
 * </ul>
 *
 * <p>{@link com.example.memoryserver.audit.AuditAspect AuditAspect} (and its
 * peer in {@code codeexec-server}) reads these via {@link #fromCurrentRequest()}
 * so a single root request that fans out into N subagents produces N+1 audit
 * rows that can be re-stitched into a tree on the dashboard.
 *
 * <p>Why a static helper and not a stereotype bean / filter-populated MDC?
 * <ul>
 *   <li><b>No stereotype bean</b> because we want callers to be obviously
 *       request-scoped — a Spring-managed @RequestScope bean would silently
 *       return stale data if accidentally injected into a singleton.</li>
 *   <li><b>No MDC</b> because that pollutes every log line on the server with
 *       agent-lineage fields that mean nothing for non-agent traffic. We only
 *       want the AUDIT logger to carry lineage; everything else stays clean.</li>
 *   <li><b>RequestContextHolder</b> is Spring's standard escape hatch for
 *       "give me the current HTTP request from anywhere", and it's exactly
 *       the scope the AUDIT aspect wraps — same thread, same request.</li>
 * </ul>
 *
 * <p>Safe to call from any thread that has a Spring web request bound to it
 * (i.e. inside a controller / @Around aspect). Returns {@link #ABSENT} when
 * called outside a request scope (e.g. unit tests, scheduled jobs).
 */
public final class AgentLineageContext {

    /** Header name for the root chat session id (constant across the spawn tree). */
    public static final String HEADER_ROOT_SESSION = "X-Root-Session-Id";
    /** Header name for the immediate parent's session id. */
    public static final String HEADER_PARENT_SESSION = "X-Parent-Session-Id";
    /** Header name for the recursion depth (integer). */
    public static final String HEADER_DEPTH = "X-Agent-Depth";

    /**
     * Sentinel for "no lineage information available" (no request scope, or
     * headers not sent — e.g. a direct curl against the backend, a unit test,
     * or pre-spawn legacy traffic).
     *
     * <p>Using "-" rather than {@code null} so the audit log line is always
     * structured (every field has a value) — downstream log parsers stay simple.
     */
    public static final AgentLineage ABSENT = new AgentLineage("-", "-", 0);

    private AgentLineageContext() {
        // Utility class.
    }

    /**
     * Snapshot the agent lineage from the currently-bound HTTP request, if any.
     *
     * <p>Returns {@link #ABSENT} when:
     * <ul>
     *   <li>No request is bound to the current thread (out-of-request call)</li>
     *   <li>The bound attributes are not servlet attributes</li>
     *   <li>All three headers are missing</li>
     * </ul>
     *
     * <p>Returns a partially-populated record when SOME headers are present
     * — e.g. a depth=0 root request might send only X-Root-Session-Id and
     * X-Agent-Depth; X-Parent-Session-Id can sensibly equal root in that
     * case, but we don't fabricate it here. The Python side is the source
     * of truth for what's emitted.
     */
    public static AgentLineage fromCurrentRequest() {
        // RequestContextHolder is thread-local. A non-web call (scheduled
        // task, unit test, plain main) returns null here.
        RequestAttributes attrs = RequestContextHolder.getRequestAttributes();
        if (!(attrs instanceof ServletRequestAttributes sra)) {
            return ABSENT;
        }

        HttpServletRequest req = sra.getRequest();
        String root = req.getHeader(HEADER_ROOT_SESSION);
        String parent = req.getHeader(HEADER_PARENT_SESSION);
        String depthStr = req.getHeader(HEADER_DEPTH);

        // If none of the lineage headers were sent, this isn't an agent
        // call — return the sentinel rather than a triple-"-" record so
        // the caller can distinguish "no lineage" from "lineage=root".
        if (root == null && parent == null && depthStr == null) {
            return ABSENT;
        }

        int depth = parseDepth(depthStr);
        return new AgentLineage(
                root != null ? root : "-",
                parent != null ? parent : "-",
                depth
        );
    }

    /**
     * Parse a depth header value, defending against malformed input. We don't
     * raise on garbage because the audit aspect must never fail a request
     * over an observability concern — wrap it in try/catch and fall back to 0.
     */
    private static int parseDepth(String depthStr) {
        if (depthStr == null || depthStr.isBlank()) {
            return 0;
        }
        try {
            int d = Integer.parseInt(depthStr.trim());
            // Clamp to a sane range. Negatives or huge values indicate a
            // misconfigured client; cap so the audit log never carries
            // ridiculous values that confuse downstream parsers.
            if (d < 0) return 0;
            if (d > 99) return 99;
            return d;
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    /**
     * Immutable lineage triple. Fields default to {@code "-"} / {@code 0}
     * when the corresponding header is absent.
     */
    public record AgentLineage(String rootSessionId, String parentSessionId, int depth) {
        public boolean isAbsent() {
            // Cheap shape check used by tests and by the audit aspect for
            // log-level decisions ("downgrade to DEBUG when nothing useful").
            return "-".equals(rootSessionId)
                    && "-".equals(parentSessionId)
                    && depth == 0;
        }
    }
}
