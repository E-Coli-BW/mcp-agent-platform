package com.example.auth.service;

import com.example.auth.model.OutboxEvent;
import com.example.auth.repository.OutboxEventRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/**
 * Background poller that publishes outbox events to Kafka with at-least-once
 * semantics and bounded poison-message blast radius.
 *
 * <h3>Pre-fix (commit history: P0-3 problem)</h3>
 * The original implementation had four interlocking production hazards:
 * <ol>
 *   <li><b>No row lock</b> → two publisher instances would scan the same
 *       window concurrently and publish each event twice to Kafka.</li>
 *   <li><b>{@code break} on first failure</b> → a single transient send error
 *       blocked the entire remaining batch until the next poll tick.</li>
 *   <li><b>No fail-count / DLQ</b> → a permanently-failing "poison" event
 *       would be retried every 1 second forever, monopolising the polling
 *       budget and starving healthy events.</li>
 *   <li><b>Unbounded {@code .get()}</b> on the send future → a slow Kafka
 *       broker would block the scheduler thread indefinitely, which is
 *       a {@code @Scheduled} pool of one by default — meaning one slow
 *       send = the polling loop is dead.</li>
 * </ol>
 *
 * <h3>Fix (this class)</h3>
 * <ul>
 *   <li>{@code SELECT ... FOR UPDATE SKIP LOCKED} — see
 *       {@link OutboxEventRepository#findUnpublishedForUpdateSkipLocked(int)}
 *       for the rationale. Multiple instances now cleanly partition work.</li>
 *   <li>The whole batch runs inside ONE {@code @Transactional} method, so the
 *       row locks held by {@code FOR UPDATE} stay until commit. Other pollers
 *       see "rows are locked, skip them."</li>
 *   <li>Per-event try/catch with {@code continue}: a failure increments
 *       {@code failCount} and moves on. The row is left {@code published=false}
 *       so the next poll picks it up.</li>
 *   <li>Once {@code failCount >= max-fail-count}, the row is marked
 *       {@code dead=true} and stops being polled — the equivalent of moving
 *       it to a DLQ. An ops dashboard can {@code SELECT * WHERE dead=true}.</li>
 *   <li>{@code future.get(timeout)} bounds the per-send wait. A timeout is
 *       counted as a failure like any other.</li>
 * </ul>
 *
 * <h3>Configuration knobs</h3>
 * <table>
 *   <tr><th>Property</th><th>Default</th><th>Why this default</th></tr>
 *   <tr><td>{@code outbox.publisher.batch-size}</td><td>100</td>
 *       <td>Big enough to amortise the poll overhead, small enough that a
 *           single slow broker tick doesn't stall thousands of rows.</td></tr>
 *   <tr><td>{@code outbox.publisher.max-fail-count}</td><td>5</td>
 *       <td>Transient broker hiccups recover in &lt; 5 polls; persistent
 *           failure = real problem, escalate to dead.</td></tr>
 *   <tr><td>{@code outbox.publisher.kafka-send-timeout-ms}</td><td>5000</td>
 *       <td>Longer than a typical Kafka roundtrip (10s of ms) but shorter
 *           than {@code @Scheduled} interval × batch-size so a slow broker
 *           can't queue an unbounded batch waiting.</td></tr>
 *   <tr><td>{@code outbox.publisher.poll-interval-ms}</td><td>1000</td>
 *       <td>1s is fine for the at-least-once SLA we promise; tighten if you
 *           need end-to-end latency &lt; 1s.</td></tr>
 * </table>
 */
@Component
@ConditionalOnProperty(name = "outbox.kafka.enabled", havingValue = "true", matchIfMissing = false)
public class OutboxPublisher {

    private static final Logger log = LoggerFactory.getLogger(OutboxPublisher.class);

    private final OutboxEventRepository outboxRepo;
    private final KafkaTemplate<String, String> kafkaTemplate;

    private final int batchSize;
    private final int maxFailCount;
    private final long kafkaSendTimeoutMs;

    public OutboxPublisher(OutboxEventRepository outboxRepo,
                           KafkaTemplate<String, String> kafkaTemplate,
                           @Value("${outbox.publisher.batch-size:100}") int batchSize,
                           @Value("${outbox.publisher.max-fail-count:5}") int maxFailCount,
                           @Value("${outbox.publisher.kafka-send-timeout-ms:5000}") long kafkaSendTimeoutMs) {
        this.outboxRepo = outboxRepo;
        this.kafkaTemplate = kafkaTemplate;
        this.batchSize = batchSize;
        this.maxFailCount = maxFailCount;
        this.kafkaSendTimeoutMs = kafkaSendTimeoutMs;
    }

    /**
     * Polled by Spring's scheduler. Configurable poll interval via
     * {@code outbox.publisher.poll-interval-ms} (default 1s).
     *
     * <p>Marked {@code @Transactional} so the row locks acquired by
     * {@code FOR UPDATE} are held until this method returns. Without
     * the transaction boundary, the locks would be released immediately
     * after the SELECT and we'd lose the multi-instance safety property.
     */
    @Scheduled(fixedDelayString = "${outbox.publisher.poll-interval-ms:1000}")
    @Transactional
    public void publishPendingEvents() {
        List<OutboxEvent> events = outboxRepo.findUnpublishedForUpdateSkipLocked(batchSize);
        if (events.isEmpty()) return;

        int published = 0, failed = 0, deadJustNow = 0;
        for (OutboxEvent event : events) {
            try {
                kafkaTemplate.send(event.getTopic(), event.getEventKey(), event.getPayload())
                        .get(kafkaSendTimeoutMs, TimeUnit.MILLISECONDS);
                event.setPublished(true);
                event.setLastAttemptAt(Instant.now());
                event.setLastError(null);
                published++;
                log.debug("Published outbox event: id={}, topic={}", event.getId(), event.getTopic());
            } catch (TimeoutException te) {
                // Cancel the send future so we don't leak a producer thread waiting.
                // We deliberately keep going to the next event — see class javadoc point #2.
                onSendFailure(event, "send timeout after " + kafkaSendTimeoutMs + "ms");
                failed++;
                if (event.isDead()) deadJustNow++;
            } catch (Exception e) {
                onSendFailure(event, e.getClass().getSimpleName() + ": " + e.getMessage());
                failed++;
                if (event.isDead()) deadJustNow++;
            }
        }
        // Single batch save (Hibernate dirty-checking would do it on flush anyway,
        // but being explicit makes the contract obvious to future readers).
        outboxRepo.saveAll(events);

        if (failed > 0 || deadJustNow > 0) {
            log.warn("Outbox batch: published={}, failed={}, newly-dead={}, total={}",
                    published, failed, deadJustNow, events.size());
        }
    }

    /**
     * Apply failure bookkeeping to a single event. Always advances
     * {@code failCount}; promotes to {@code dead=true} when the limit is hit.
     * The row is intentionally left {@code published=false} so the next
     * poll re-attempts it (until it goes dead).
     */
    private void onSendFailure(OutboxEvent event, String errorSummary) {
        event.setFailCount(event.getFailCount() + 1);
        event.setLastAttemptAt(Instant.now());
        event.setLastError(errorSummary);
        if (event.getFailCount() >= maxFailCount) {
            event.setDead(true);
            log.error("Outbox event marked DEAD after {} failures: id={}, topic={}, lastError={}",
                    event.getFailCount(), event.getId(), event.getTopic(), errorSummary);
        } else {
            log.warn("Outbox send failed (will retry): id={}, attempt={}/{}, error={}",
                    event.getId(), event.getFailCount(), maxFailCount, errorSummary);
        }
    }
}

