package com.example.auth.model;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;

import java.time.Instant;

/**
 * Transactional outbox event for at-least-once Kafka delivery.
 *
 * <p>Two state machines live on this row:
 * <ol>
 *   <li><b>Liveness</b>: {@code published=false} → {@code published=true}
 *       (terminal — we can purge old rows on a TTL job).</li>
 *   <li><b>Health</b>: {@code dead=false} → {@code dead=true} when
 *       {@link #failCount} crosses {@code outbox.publisher.max-fail-count}.
 *       Dead rows stop being polled by the publisher and are kept for ops
 *       inspection / manual replay.</li>
 * </ol>
 *
 * <p>The index {@code idx_outbox_unpublished} is what lets the publisher's
 * {@code FOR UPDATE SKIP LOCKED} query stay O(unpublished-count) instead of
 * O(table-size) once published rows accumulate.
 */
@Entity
@Table(name = "outbox_events", indexes = {
        // Partial-ish index — H2/MySQL don't support partial indexes, but the
        // (published, dead, created_at) ordering still lets the planner do
        // an index range scan on the (false, false, ...) prefix.
        @Index(name = "idx_outbox_unpublished",
                columnList = "published, dead, created_at")
})
public class OutboxEvent {
    @Id
    @Column(length = 36)
    private String id;

    @Column(nullable = false, length = 100)
    private String topic;

    @Column(name = "event_key", length = 100)
    private String eventKey;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String payload;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(nullable = false)
    private boolean published = false;

    /**
     * How many times the publisher has tried (and failed) to send this event.
     * Incremented on every failed Kafka send; reset is implicit (row gets
     * {@code published=true} on success and is no longer polled).
     */
    @Column(name = "fail_count", nullable = false)
    private int failCount = 0;

    /**
     * True when {@code failCount >= max-fail-count}. Dead rows are excluded
     * from the publisher's query so a single poison message can't burn through
     * the polling budget every second forever.
     */
    @Column(nullable = false)
    private boolean dead = false;

    /** Last time the publisher attempted this event — useful for ops triage. */
    @Column(name = "last_attempt_at")
    private Instant lastAttemptAt;

    /**
     * Truncated error message from the last send failure. Bounded to keep
     * the row size predictable; full stack traces belong in logs.
     */
    @Column(name = "last_error", length = 500)
    private String lastError;

    public OutboxEvent() {}

    public OutboxEvent(String id, String topic, String eventKey, String payload) {
        this.id = id;
        this.topic = topic;
        this.eventKey = eventKey;
        this.payload = payload;
        this.createdAt = Instant.now();
    }

    @PrePersist
    protected void onCreate() {
        if (createdAt == null) createdAt = Instant.now();
    }

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }
    public String getTopic() { return topic; }
    public void setTopic(String topic) { this.topic = topic; }
    public String getEventKey() { return eventKey; }
    public void setEventKey(String eventKey) { this.eventKey = eventKey; }
    public String getPayload() { return payload; }
    public void setPayload(String payload) { this.payload = payload; }
    public Instant getCreatedAt() { return createdAt; }
    public boolean isPublished() { return published; }
    public void setPublished(boolean published) { this.published = published; }

    public int getFailCount() { return failCount; }
    public void setFailCount(int failCount) { this.failCount = failCount; }
    public boolean isDead() { return dead; }
    public void setDead(boolean dead) { this.dead = dead; }
    public Instant getLastAttemptAt() { return lastAttemptAt; }
    public void setLastAttemptAt(Instant lastAttemptAt) { this.lastAttemptAt = lastAttemptAt; }
    public String getLastError() { return lastError; }
    public void setLastError(String lastError) {
        // Defensive truncation — protect the column length contract from
        // exotic Kafka error messages with megabyte stack traces.
        if (lastError != null && lastError.length() > 500) {
            this.lastError = lastError.substring(0, 497) + "...";
        } else {
            this.lastError = lastError;
        }
    }
}

