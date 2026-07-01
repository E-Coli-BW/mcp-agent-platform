package com.example.memorystore.kafka;

import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.kafka.support.KafkaHeaders;
import org.springframework.messaging.handler.annotation.Header;
import org.springframework.messaging.handler.annotation.Payload;
import org.springframework.stereotype.Component;

import java.time.Duration;

/**
 * Kafka consumer for user lifecycle events (from auth-service outbox).
 *
 * Pattern: Transactional Outbox → Kafka → Idempotent Consumer
 *
 * Flow:
 * 1. auth-service writes OutboxEvent to DB in same transaction as user signup
 * 2. OutboxPublisher polls DB and sends to Kafka topic "user.events"
 * 3. This consumer receives the event and auto-provisions user workspace
 *
 * Idempotency:
 * - Each event has a unique ID (from OutboxEvent.id = UUID)
 * - We track processed IDs in a bounded Caffeine cache (LRU + TTL)
 * - Duplicate delivery → skip processing, still ACK
 *
 * Why Caffeine (not ConcurrentHashMap):
 * - ConcurrentHashMap grows forever → OOM under sustained load
 *   (a UUID entry is ~80 bytes, 1k events/s = 6.9 GB/day)
 * - Caffeine bounds the cache by both size and age:
 *     - maximumSize: hard upper bound (entries evicted in LRU order)
 *     - expireAfterWrite: bounds the duplicate-detection window
 *   The window MUST be larger than Kafka's max retention + redelivery
 *   delay so a redelivered event still finds its sentinel.
 *
 * Why manual ACK:
 * - We only commit offset AFTER successful processing
 * - If processing fails mid-way, Kafka redelivers (at-least-once)
 * - Idempotency guard ensures exactly-once semantics at application level
 *
 * Multi-instance note:
 * - Caffeine is process-local. Two consumer instances do NOT share dedup
 *   state. For Kafka, this is acceptable because the consumer group
 *   guarantees each partition is owned by exactly one instance — so the
 *   same eventId will be delivered to the same instance (assuming no
 *   rebalance during the dedup window).
 * - During rebalance, the new owner has an empty cache and may re-process
 *   in-flight events. The downstream handler MUST be idempotent regardless.
 * - For strict cross-instance dedup, swap this cache for a Redis SETEX
 *   backend (key = "event:processed:{uuid}", TTL = dedupWindow).
 */
@Component
@ConditionalOnProperty(name = "kafka.consumer.enabled", havingValue = "true", matchIfMissing = false)
public class UserEventConsumer {

    private static final Logger log = LoggerFactory.getLogger(UserEventConsumer.class);

    /**
     * Processed event IDs for deduplication.
     *
     * Defaults sized for ~10k events/s sustained:
     *   maximumSize  = 200_000 entries  (~16 MB worst case)
     *   expireAfter  = 48 h             (must exceed Kafka retention + redelivery)
     *
     * Both are overridable via:
     *   kafka.consumer.dedup.max-size
     *   kafka.consumer.dedup.ttl
     */
    private final Cache<String, Boolean> processedEvents;

    private final UserEventHandler eventHandler;

    public UserEventConsumer(
            UserEventHandler eventHandler,
            @Value("${kafka.consumer.dedup.max-size:200000}") long dedupMaxSize,
            @Value("${kafka.consumer.dedup.ttl:PT48H}") Duration dedupTtl) {
        this.eventHandler = eventHandler;
        this.processedEvents = Caffeine.newBuilder()
                .maximumSize(dedupMaxSize)
                .expireAfterWrite(dedupTtl)
                .recordStats()
                .build();
        log.info("🛡️  Kafka idempotency cache: maxSize={}, ttl={}", dedupMaxSize, dedupTtl);
    }

    @KafkaListener(
            topics = "user.events",
            groupId = "${kafka.consumer.group-id:memory-server}",
            containerFactory = "kafkaListenerContainerFactory"
    )
    public void onUserEvent(
            @Payload String payload,
            @Header(KafkaHeaders.RECEIVED_KEY) String eventKey,
            @Header(KafkaHeaders.RECEIVED_TOPIC) String topic,
            @Header(KafkaHeaders.OFFSET) long offset,
            Acknowledgment ack
    ) {
        // Extract event ID from payload for deduplication
        String eventId = extractEventId(payload);

        // ─── Idempotency Check ─────────────────────────────────────────
        // Caffeine's asMap() view is a ConcurrentMap — putIfAbsent is atomic
        // and returns the prior value (or null if we won the race).
        if (eventId != null
                && processedEvents.asMap().putIfAbsent(eventId, Boolean.TRUE) != null) {
            log.info("Duplicate event skipped: id={}, topic={}, offset={}", eventId, topic, offset);
            ack.acknowledge();
            return;
        }

        log.info("Processing user event: topic={}, key={}, offset={}", topic, eventKey, offset);

        try {
            eventHandler.handle(eventKey, payload);
            ack.acknowledge();
            log.debug("Event processed successfully: id={}, offset={}", eventId, offset);
        } catch (Exception e) {
            log.error("Failed to process user event: id={}, offset={}, error={}",
                    eventId, offset, e.getMessage(), e);
            // Roll back the dedup sentinel — otherwise the redelivery will
            // be silently skipped as "duplicate" and the event is lost.
            // (This bug existed in the ConcurrentHashMap version too; fixed here.)
            if (eventId != null) {
                processedEvents.invalidate(eventId);
            }
            // Don't ACK — Kafka will redeliver after backoff (configured in KafkaConsumerConfig)
            throw e;
        }
    }

    /**
     * Extract event ID from JSON payload (minimal parsing, no Jackson dependency).
     * Looks for: "eventId":"uuid-value" or "id":"uuid-value"
     */
    private String extractEventId(String payload) {
        // Try "eventId" first, then "id"
        String id = extractJsonField(payload, "eventId");
        if (id == null) {
            id = extractJsonField(payload, "id");
        }
        return id;
    }

    private String extractJsonField(String json, String field) {
        String key = "\"" + field + "\"";
        int idx = json.indexOf(key);
        if (idx < 0) return null;
        int colonIdx = json.indexOf(':', idx);
        if (colonIdx < 0) return null;
        int quoteStart = json.indexOf('"', colonIdx);
        if (quoteStart < 0) return null;
        int quoteEnd = json.indexOf('"', quoteStart + 1);
        if (quoteEnd < 0) return null;
        return json.substring(quoteStart + 1, quoteEnd);
    }

    // For testing: check if an event was processed
    boolean isProcessed(String eventId) {
        return processedEvents.getIfPresent(eventId) != null;
    }

    // For testing: reset state
    void resetProcessedEvents() {
        processedEvents.invalidateAll();
    }

    /** Visible for monitoring/metrics. */
    public long dedupCacheSize() {
        return processedEvents.estimatedSize();
    }
}
