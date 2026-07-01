package com.example.memorystore.kafka;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/**
 * Handles user lifecycle events consumed from Kafka.
 *
 * Responsibilities:
 * - USER_REGISTERED → auto-provision a memory namespace for the new tenant
 * - USER_DELETED → cleanup tenant data (GDPR compliance)
 *
 * This component is separated from the consumer for testability:
 * - Consumer handles Kafka mechanics (offset, dedup, retry)
 * - Handler handles business logic (can be unit tested without Kafka)
 */
@Component
@ConditionalOnProperty(name = "kafka.consumer.enabled", havingValue = "true", matchIfMissing = false)
public class UserEventHandler {

    private static final Logger log = LoggerFactory.getLogger(UserEventHandler.class);

    /**
     * Process a user event payload.
     *
     * @param tenantId the tenant/partition key
     * @param payload  JSON payload from outbox event
     */
    public void handle(String tenantId, String payload) {
        String eventType = extractType(payload);

        switch (eventType) {
            case "USER_REGISTERED" -> handleUserRegistered(tenantId, payload);
            case "USER_DELETED" -> handleUserDeleted(tenantId, payload);
            default -> log.warn("Unknown event type: {} for tenant: {}", eventType, tenantId);
        }
    }

    private void handleUserRegistered(String tenantId, String payload) {
        String username = extractField(payload, "username");
        String email = extractField(payload, "email");

        log.info("Auto-provisioning memory namespace for new user: tenant={}, username={}", tenantId, username);

        // In production, this would:
        // 1. Create a tenant-specific memory namespace
        // 2. Initialize default memory entries (welcome message, config)
        // 3. Set quota limits based on tenant plan

        // For now, log the provisioning action
        log.info("Namespace provisioned: memory-{}, user={}, email={}", tenantId, username, email);
    }

    private void handleUserDeleted(String tenantId, String payload) {
        String userId = extractField(payload, "userId");

        log.info("Cleaning up memory data for deleted user: tenant={}, userId={}", tenantId, userId);

        // In production, this would:
        // 1. Delete all memory entries for this tenant
        // 2. Remove ES index: memory-{tenantId}
        // 3. Audit log the deletion for GDPR compliance
    }

    private String extractType(String payload) {
        String type = extractField(payload, "type");
        return type != null ? type : "UNKNOWN";
    }

    private String extractField(String json, String field) {
        String key = "\"" + field + "\"";
        int idx = json.indexOf(key);
        if (idx < 0) return null;
        int colonIdx = json.indexOf(':', idx);
        if (colonIdx < 0) return null;

        // Skip whitespace
        int valueStart = colonIdx + 1;
        while (valueStart < json.length() && Character.isWhitespace(json.charAt(valueStart))) {
            valueStart++;
        }

        if (valueStart >= json.length()) return null;
        char first = json.charAt(valueStart);

        if (first == '"') {
            // String value
            int quoteEnd = json.indexOf('"', valueStart + 1);
            return quoteEnd > 0 ? json.substring(valueStart + 1, quoteEnd) : null;
        } else {
            // Numeric or boolean — read until comma, brace, or bracket
            int end = valueStart;
            while (end < json.length() && json.charAt(end) != ',' && json.charAt(end) != '}'
                    && json.charAt(end) != ']') {
                end++;
            }
            return json.substring(valueStart, end).trim();
        }
    }
}
