package com.example.memoryserver.security;

import com.example.mcp.common.security.AgentLineageContext;
import com.example.mcp.common.security.AgentLineageContext.AgentLineage;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link AgentLineageContext}.
 *
 * <p>Lives in memory-server (not mcp-common) so it shares the same test
 * infrastructure as {@link TenantContextTest} — mcp-common has no test
 * sources and adding one just for this would force a new junit dependency
 * declaration there.
 *
 * <p>These tests are pure unit (no Spring context) — they bind a
 * {@link MockHttpServletRequest} to the thread via
 * {@link RequestContextHolder} the same way Spring's
 * {@code RequestContextFilter} does in production.
 */
class AgentLineageContextTest {

    @AfterEach
    void cleanup() {
        // RequestContextHolder is thread-local; clear it so a leaked binding
        // from one test never bleeds into the next.
        RequestContextHolder.resetRequestAttributes();
    }

    private static void bindRequest(MockHttpServletRequest req) {
        RequestContextHolder.setRequestAttributes(new ServletRequestAttributes(req));
    }

    // ── Happy path ──────────────────────────────────────────────

    @Test
    void fromCurrentRequest_returnsAllThreeFields_whenAllHeadersPresent() {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-42");
        req.addHeader(AgentLineageContext.HEADER_PARENT_SESSION, "chat-42/sub-abc");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "2");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();

        assertEquals("chat-42", lineage.rootSessionId());
        assertEquals("chat-42/sub-abc", lineage.parentSessionId());
        assertEquals(2, lineage.depth());
        assertFalse(lineage.isAbsent());
    }

    @Test
    void fromCurrentRequest_treatsDepthZero_asRootRequest() {
        // depth=0 is the most common case (every root user request).
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_PARENT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "0");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();
        assertEquals(0, lineage.depth());
        // depth=0 but with a session id => NOT "absent" (absent means triple-default)
        assertFalse(lineage.isAbsent());
    }

    // ── Absent / no-request scopes ──────────────────────────────

    @Test
    void fromCurrentRequest_returnsAbsent_whenNoRequestBound() {
        // No setRequestAttributes() call before this — simulates a
        // scheduled task or unit-test caller.
        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();

        assertSame(AgentLineageContext.ABSENT, lineage);
        assertTrue(lineage.isAbsent());
    }

    @Test
    void fromCurrentRequest_returnsAbsent_whenAllHeadersMissing() {
        // A direct curl with no lineage headers — happens for non-agent
        // traffic. We want ABSENT (the sentinel), not a triple-"-" record.
        MockHttpServletRequest req = new MockHttpServletRequest();
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();

        assertSame(AgentLineageContext.ABSENT, lineage);
    }

    // ── Partial / malformed input — defensive parsing ────────────

    @Test
    void fromCurrentRequest_fillsMissingFieldsWithDash_whenSomeHeadersPresent() {
        // At least one lineage header present => we return a populated
        // record (not ABSENT), but missing fields default to "-".
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-99");
        // No parent header, no depth header.
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();

        assertEquals("chat-99", lineage.rootSessionId());
        assertEquals("-", lineage.parentSessionId());
        assertEquals(0, lineage.depth());
        assertFalse(lineage.isAbsent()); // root_session is set => not absent
    }

    @Test
    void parseDepth_garbageInput_fallsBackToZero() {
        // The audit aspect must never fail a request over an
        // observability concern — bad depth header => default 0.
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "not-a-number");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();
        assertEquals(0, lineage.depth());
    }

    @Test
    void parseDepth_negativeInput_clampsToZero() {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "-5");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();
        assertEquals(0, lineage.depth());
    }

    @Test
    void parseDepth_huge_clampsTo99() {
        // Defensive: a misconfigured client sending depth=99999 would
        // make audit log columns variable-width. Clamp.
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "999999");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();
        assertEquals(99, lineage.depth());
    }

    @Test
    void parseDepth_blank_fallsBackToZero() {
        MockHttpServletRequest req = new MockHttpServletRequest();
        req.addHeader(AgentLineageContext.HEADER_ROOT_SESSION, "chat-1");
        req.addHeader(AgentLineageContext.HEADER_DEPTH, "   ");
        bindRequest(req);

        AgentLineage lineage = AgentLineageContext.fromCurrentRequest();
        assertEquals(0, lineage.depth());
    }

    // ── ABSENT sentinel shape ────────────────────────────────────

    @Test
    void absentSentinel_hasDashesAndZero() {
        // Lock the field shape so any downstream log parser written today
        // against "-" / "-" / 0 will keep working tomorrow.
        assertEquals("-", AgentLineageContext.ABSENT.rootSessionId());
        assertEquals("-", AgentLineageContext.ABSENT.parentSessionId());
        assertEquals(0, AgentLineageContext.ABSENT.depth());
        assertTrue(AgentLineageContext.ABSENT.isAbsent());
    }

    @Test
    void headerNameConstants_matchWireFormat() {
        // Names are part of our service-to-service contract — if they ever
        // change we must update the Python side in lockstep. This test
        // pins them so renaming forces a deliberate update.
        assertEquals("X-Root-Session-Id", AgentLineageContext.HEADER_ROOT_SESSION);
        assertEquals("X-Parent-Session-Id", AgentLineageContext.HEADER_PARENT_SESSION);
        assertEquals("X-Agent-Depth", AgentLineageContext.HEADER_DEPTH);
    }
}
