package com.example.memoryserver.event;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Kafka consumer for agent tool events — audit log + analytics.
 *
 * WHAT THIS DOES:
 * - Consumes events from "agent.tool.events" and "agent.responses" topics
 * - Tracks per-session tool usage statistics
 * - Logs events for audit trail
 * - Could write to a database for dashboards (simplified here to in-memory)
 *
 * WHY IN MEMORY-SERVER?
 * The memory server already has PostgreSQL for persistence. Adding a
 * tool_audit table would be trivial. For now, we track in-memory stats
 * and log events. In production, this would be a separate audit-service.
 *
 * CONDITIONAL: Only activates when spring.kafka.bootstrap-servers is set.
 */
@Component
@ConditionalOnProperty(name = "spring.kafka.bootstrap-servers")
public class ToolEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(ToolEventConsumer.class);
    private final ObjectMapper mapper;

    // In-memory analytics (production: write to PostgreSQL)
    private final AtomicLong totalToolCalls = new AtomicLong();
    private final AtomicLong totalResponses = new AtomicLong();
    private final AtomicLong totalTokens = new AtomicLong();
    private final ConcurrentHashMap<String, AtomicLong> toolUsageCount = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, AtomicLong> sessionToolCount = new ConcurrentHashMap<>();

    public ToolEventConsumer(ObjectMapper mapper) {
        this.mapper = mapper;
        log.info("ToolEventConsumer initialized — listening on agent.tool.events, agent.responses");
    }

    @KafkaListener(topics = "agent.tool.events", groupId = "memory-server-audit")
    public void onToolEvent(String message) {
        try {
            ToolEventRecord event = mapper.readValue(message, ToolEventRecord.class);

            if ("tool_start".equals(event.eventType())) {
                totalToolCalls.incrementAndGet();
                toolUsageCount
                        .computeIfAbsent(event.toolName(), k -> new AtomicLong())
                        .incrementAndGet();
                sessionToolCount
                        .computeIfAbsent(event.sessionId(), k -> new AtomicLong())
                        .incrementAndGet();

                log.info("🔧 Tool event: session={} tool={} input={}",
                        event.sessionId(), event.toolName(),
                        event.toolInput() != null ? event.toolInput().toString().substring(0, Math.min(100, event.toolInput().toString().length())) : "");
            } else if ("tool_end".equals(event.eventType())) {
                log.debug("Tool completed: {} in {}ms", event.toolName(), event.durationMs());
            }
        } catch (Exception e) {
            log.warn("Failed to process tool event: {}", e.getMessage());
        }
    }

    @KafkaListener(topics = "agent.responses", groupId = "memory-server-audit")
    public void onAgentResponse(String message) {
        try {
            ToolEventRecord event = mapper.readValue(message, ToolEventRecord.class);
            totalResponses.incrementAndGet();
            totalTokens.addAndGet(event.tokenCount());

            log.info("📊 Agent response: session={} model={} tokens={} duration={}ms",
                    event.sessionId(), event.model(), event.tokenCount(), event.durationMs());
        } catch (Exception e) {
            log.warn("Failed to process response event: {}", e.getMessage());
        }
    }

    // ── Metrics accessors (for health/actuator endpoint) ──

    public long getTotalToolCalls() { return totalToolCalls.get(); }
    public long getTotalResponses() { return totalResponses.get(); }
    public long getTotalTokens() { return totalTokens.get(); }
    public ConcurrentHashMap<String, AtomicLong> getToolUsageCount() { return toolUsageCount; }
    public ConcurrentHashMap<String, AtomicLong> getSessionToolCount() { return sessionToolCount; }
}
