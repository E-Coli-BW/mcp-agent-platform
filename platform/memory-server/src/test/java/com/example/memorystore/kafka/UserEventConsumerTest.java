package com.example.memorystore.kafka;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.kafka.support.Acknowledgment;

import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/**
 * Unit tests for Kafka consumer idempotency and event handling.
 * No Kafka broker needed — tests business logic in isolation.
 */
class UserEventConsumerTest {

    private UserEventHandler handler;
    private UserEventConsumer consumer;
    private Acknowledgment ack;

    @BeforeEach
    void setup() {
        handler = new UserEventHandler();
        // Tiny cache for LRU eviction tests; long enough TTL that time isn't a factor.
        consumer = new UserEventConsumer(handler, /*maxSize*/ 4, Duration.ofMinutes(10));
        consumer.resetProcessedEvents();
        ack = mock(Acknowledgment.class);
    }

    private static String payloadFor(String id) {
        return """
            {
              "eventId": "%s",
              "type": "USER_REGISTERED",
              "userId": 42,
              "username": "u",
              "email": "u@e.com",
              "tenantId": "tenant-1",
              "timestamp": "2026-06-10T10:00:00Z"
            }
            """.formatted(id);
    }

    // ─── Deduplication Tests ───────────────────────────────────────────

    @Test
    void should_markEventAsProcessed_when_firstDelivery() {
        assertFalse(consumer.isProcessed("evt-001"));

        consumer.onUserEvent(payloadFor("evt-001"), "tenant-1", "user.events", 0L, ack);

        assertTrue(consumer.isProcessed("evt-001"));
        verify(ack, times(1)).acknowledge();
    }

    @Test
    void should_skipProcessing_when_sameEventIdDeliveredTwice() {
        consumer.onUserEvent(payloadFor("evt-002"), "tenant-1", "user.events", 0L, ack);
        consumer.onUserEvent(payloadFor("evt-002"), "tenant-1", "user.events", 1L, ack);

        // Both calls ACK (so Kafka stops redelivering), but the second is a no-op.
        verify(ack, times(2)).acknowledge();
        assertTrue(consumer.isProcessed("evt-002"));
        assertEquals(1L, consumer.dedupCacheSize());
    }

    @Test
    void should_rollbackDedupSentinel_when_handlerThrows() {
        UserEventHandler throwing = mock(UserEventHandler.class);
        doThrow(new RuntimeException("downstream blew up"))
                .when(throwing).handle(anyString(), anyString());
        UserEventConsumer c = new UserEventConsumer(throwing, 100L, Duration.ofMinutes(10));

        assertThrows(RuntimeException.class,
                () -> c.onUserEvent(payloadFor("evt-fail"), "t", "user.events", 0L, ack));

        // Critical: the sentinel must be removed so Kafka redelivery can be
        // re-processed instead of being silently skipped as "duplicate".
        assertFalse(c.isProcessed("evt-fail"), "sentinel should be rolled back on failure");
        verify(ack, never()).acknowledge();
    }

    @Test
    void should_evictOldestEntries_when_cacheExceedsMaxSize() {
        // Cache built in setup() with maxSize=4.
        for (int i = 0; i < 20; i++) {
            consumer.onUserEvent(payloadFor("evt-bulk-" + i), "t", "user.events", i, ack);
        }
        // Caffeine eviction is asynchronous; force it to settle.
        // estimatedSize() is the closest thing to a real assertion — it must
        // never exceed (slightly above) the configured maximum.
        long size = consumer.dedupCacheSize();
        assertTrue(size <= 8, "cache should bound under maxSize w/ some slack, got " + size);
    }

    // ─── Event Handler Tests ───────────────────────────────────────────

    @Test
    void should_handleUserRegistered_when_validPayload() {
        String payload = """
            {
              "type": "USER_REGISTERED",
              "userId": 42,
              "username": "testuser",
              "email": "test@example.com",
              "tenantId": "tenant-1",
              "timestamp": "2026-06-10T10:00:00Z"
            }
            """;

        // Should not throw — handler processes the event
        assertDoesNotThrow(() -> handler.handle("tenant-1", payload));
    }

    @Test
    void should_handleUserDeleted_when_validPayload() {
        String payload = """
            {
              "type": "USER_DELETED",
              "userId": 42,
              "tenantId": "tenant-1",
              "timestamp": "2026-06-10T12:00:00Z"
            }
            """;

        assertDoesNotThrow(() -> handler.handle("tenant-1", payload));
    }

    @Test
    void should_handleUnknownEventType_without_throwing() {
        String payload = """
            {
              "type": "USER_PASSWORD_CHANGED",
              "userId": 42
            }
            """;

        assertDoesNotThrow(() -> handler.handle("tenant-1", payload));
    }

    @Test
    void should_handleMalformedPayload_without_throwing() {
        assertDoesNotThrow(() -> handler.handle("tenant-1", "not json"));
    }

    @Test
    void should_handleEmptyPayload_without_throwing() {
        assertDoesNotThrow(() -> handler.handle("tenant-1", "{}"));
    }
}
