package com.example.auth.service;

import com.example.auth.model.OutboxEvent;
import com.example.auth.repository.OutboxEventRepository;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.RecordMetadata;
import org.apache.kafka.common.TopicPartition;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.OverrideAutoConfiguration;
import org.springframework.boot.test.autoconfigure.jdbc.AutoConfigureTestDatabase;
import org.springframework.boot.test.autoconfigure.orm.jpa.DataJpaTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Import;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.SendResult;
import org.springframework.test.context.TestPropertySource;

import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

/**
 * Tests for {@link OutboxPublisher} — the P0-3 SKIP LOCKED + DLQ rewrite.
 *
 * <p>Uses {@code @DataJpaTest} (not full {@code @SpringBootTest}) for two reasons:
 * <ol>
 *   <li>Faster — no need to spin up the full auth-service context.</li>
 *   <li>Lets us define a mock {@code KafkaTemplate} bean WITHOUT activating
 *       the real Spring Kafka auto-config (which would try to connect to
 *       a broker that isn't there in unit tests).</li>
 * </ol>
 *
 * <p>Crucially we use {@code MODE=PostgreSQL} for the H2 URL — H2 only
 * recognises {@code FOR UPDATE SKIP LOCKED} in Postgres compat mode, which
 * is also what the prod Postgres database uses. Tests this way exercise
 * exactly the SQL prod runs.
 */
@DataJpaTest
@AutoConfigureTestDatabase(replace = AutoConfigureTestDatabase.Replace.NONE)
@OverrideAutoConfiguration(enabled = false)
@TestPropertySource(properties = {
        "spring.datasource.url=jdbc:h2:mem:auth-service-outbox-publisher-test;MODE=PostgreSQL;DB_CLOSE_DELAY=-1",
        "spring.datasource.driver-class-name=org.h2.Driver",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "spring.jpa.hibernate.ddl-auto=create-drop",
        "spring.jpa.database-platform=org.hibernate.dialect.H2Dialect",
        "outbox.kafka.enabled=false"   // we instantiate publisher manually
})
@Import(OutboxPublisherTest.NoOpKafkaConfig.class)
class OutboxPublisherTest {

    @Autowired
    private OutboxEventRepository repo;

    @SuppressWarnings("unchecked")
    private final KafkaTemplate<String, String> mockKafka = mock(KafkaTemplate.class);

    private OutboxPublisher publisher;

    @BeforeEach
    void setup() {
        repo.deleteAll();
        // Default config — overridden per test where needed
        publisher = new OutboxPublisher(repo, mockKafka,
                /* batchSize */ 100,
                /* maxFailCount */ 5,
                /* kafkaSendTimeoutMs */ 5_000);
        // Default: every send succeeds
        when(mockKafka.send(anyString(), anyString(), anyString()))
                .thenAnswer(inv -> succeededFuture(inv.getArgument(0)));
    }

    // ── Happy path ─────────────────────────────────────────────

    @Test
    void should_publishAllPendingEvents_when_sendSucceeds() {
        seed("e1", "topic.a", "k1", "{}");
        seed("e2", "topic.a", "k2", "{}");
        seed("e3", "topic.b", "k3", "{}");

        publisher.publishPendingEvents();

        // All three should be marked published — pre-fix code would also pass this
        assertEquals(3L, repo.findAll().stream().filter(OutboxEvent::isPublished).count());
        verify(mockKafka, times(3)).send(anyString(), anyString(), anyString());
    }

    @Test
    void should_processNothingAndNotCallKafka_when_noPendingEvents() {
        publisher.publishPendingEvents();
        verifyNoInteractions(mockKafka);
    }

    @Test
    void should_skipPublishedEvents_when_polled() {
        OutboxEvent done = seed("done", "topic", "k", "{}");
        done.setPublished(true);
        repo.save(done);

        publisher.publishPendingEvents();
        verifyNoInteractions(mockKafka);
    }

    // ── continue-not-break (P0-3 bug #2) ──────────────────────

    @Test
    void should_continueBatch_when_singleEventFails() {
        // Pre-fix: first failure → break → e3 never tried in this batch
        seed("e1", "topic", "k", "{}");
        seed("e2", "topic", "k", "{}");
        seed("e3", "topic", "k", "{}");

        when(mockKafka.send(anyString(), anyString(), eq("{}")))
                .thenAnswer(inv -> succeededFuture(inv.getArgument(0)));
        // Make e2 fail — using payload as the discriminator is awkward, so we
        // fail by sequence: 1st succeeds, 2nd fails, 3rd succeeds.
        reset(mockKafka);
        AtomicInteger calls = new AtomicInteger();
        when(mockKafka.send(anyString(), anyString(), anyString())).thenAnswer(inv -> {
            int n = calls.incrementAndGet();
            if (n == 2) return failedFuture(new RuntimeException("broker hiccup"));
            return succeededFuture(inv.getArgument(0));
        });

        publisher.publishPendingEvents();

        verify(mockKafka, times(3)).send(anyString(), anyString(), anyString());
        List<OutboxEvent> all = repo.findAll();
        long publishedCount = all.stream().filter(OutboxEvent::isPublished).count();
        long failedCount = all.stream().filter(e -> e.getFailCount() > 0).count();
        assertEquals(2, publishedCount, "two successful sends must have published=true");
        assertEquals(1, failedCount, "the one failure must have failCount=1, not block the rest");
    }

    // ── Poison message DLQ (P0-3 bug #3) ──────────────────────

    @Test
    void should_markEventDead_when_failCountReachesMax() {
        OutboxEvent poison = seed("poison", "topic", "k", "{}");
        when(mockKafka.send(anyString(), anyString(), anyString()))
                .thenAnswer(inv -> failedFuture(new RuntimeException("permanent failure")));

        // maxFailCount=5 → 5 attempts then dead
        for (int i = 0; i < 5; i++) {
            publisher.publishPendingEvents();
        }

        OutboxEvent reloaded = repo.findById(poison.getId()).orElseThrow();
        assertEquals(5, reloaded.getFailCount(), "should have exactly maxFailCount attempts");
        assertTrue(reloaded.isDead(), "row must be dead after maxFailCount failures");
        assertFalse(reloaded.isPublished(), "dead rows are NOT marked published — ops needs to see them");
        assertNotNull(reloaded.getLastError());
        assertNotNull(reloaded.getLastAttemptAt());

        // Next poll: dead row must be skipped — no further Kafka calls
        clearInvocations(mockKafka);
        publisher.publishPendingEvents();
        verifyNoInteractions(mockKafka);
    }

    @Test
    void should_keepHealthyEventsFlowing_when_poisonMessagePresent() {
        // Pre-fix: poison message in position 1 would block everything else forever
        seed("poison", "topic", "k", "{}");                  // fails always
        seed("healthy1", "topic", "k", "{\"healthy\": 1}");
        seed("healthy2", "topic", "k", "{\"healthy\": 2}");

        when(mockKafka.send(anyString(), anyString(), eq("{}")))
                .thenAnswer(inv -> failedFuture(new RuntimeException("poison")));
        when(mockKafka.send(anyString(), anyString(), org.mockito.AdditionalMatchers.not(eq("{}"))))
                .thenAnswer(inv -> succeededFuture(inv.getArgument(0)));

        publisher.publishPendingEvents();

        // Both healthy events published in the SAME batch as the poison failure
        assertTrue(repo.findById("healthy1").orElseThrow().isPublished());
        assertTrue(repo.findById("healthy2").orElseThrow().isPublished());
        assertFalse(repo.findById("poison").orElseThrow().isPublished());
        assertEquals(1, repo.findById("poison").orElseThrow().getFailCount());
    }

    @Test
    void should_truncateLongErrorMessages_when_persistingFailure() {
        seed("e1", "topic", "k", "{}");
        String hugeError = "x".repeat(10_000);
        when(mockKafka.send(anyString(), anyString(), anyString()))
                .thenAnswer(inv -> failedFuture(new RuntimeException(hugeError)));

        publisher.publishPendingEvents();

        String stored = repo.findById("e1").orElseThrow().getLastError();
        assertNotNull(stored);
        assertTrue(stored.length() <= 500,
                "lastError must be truncated to column max (500), got " + stored.length());
    }

    // ── Timeout (P0-3 bug #4) ──────────────────────────────────

    @Test
    void should_treatTimeoutAsFailure_when_kafkaIsSlow() {
        // Reconfigure publisher with very short timeout
        publisher = new OutboxPublisher(repo, mockKafka, 100, 5, /* timeout */ 50);

        seed("slow", "topic", "k", "{}");
        when(mockKafka.send(anyString(), anyString(), anyString()))
                .thenAnswer(inv -> hangForever());

        long start = System.currentTimeMillis();
        publisher.publishPendingEvents();
        long elapsed = System.currentTimeMillis() - start;

        // Must NOT have blocked indefinitely — should be ~timeout, not ~forever
        assertTrue(elapsed < 2_000, "timeout must abort the send, got elapsed=" + elapsed + "ms");

        OutboxEvent reloaded = repo.findById("slow").orElseThrow();
        assertEquals(1, reloaded.getFailCount());
        assertTrue(reloaded.getLastError().contains("timeout"),
                "timeout must surface in lastError, got: " + reloaded.getLastError());
    }

    // ── Batch size limit ──────────────────────────────────────

    @Test
    void should_respectBatchSize_when_manyEventsPending() {
        publisher = new OutboxPublisher(repo, mockKafka, /* batch */ 3, 5, 5_000);
        for (int i = 0; i < 10; i++) seed("e" + i, "topic", "k", "{}");

        publisher.publishPendingEvents();

        verify(mockKafka, times(3)).send(anyString(), anyString(), anyString());
        assertEquals(3L, repo.findAll().stream().filter(OutboxEvent::isPublished).count());
    }

    // ── helpers ────────────────────────────────────────────────

    private OutboxEvent seed(String id, String topic, String key, String payload) {
        OutboxEvent e = new OutboxEvent(id, topic, key, payload);
        return repo.save(e);
    }

    @SuppressWarnings("unchecked")
    private static CompletableFuture<SendResult<String, String>> succeededFuture(String topic) {
        // Build a minimal SendResult — we don't actually inspect it, but the
        // contract is non-null on success.
        ProducerRecord<String, String> record = new ProducerRecord<>(topic, "k", "v");
        RecordMetadata meta = new RecordMetadata(
                new TopicPartition(topic, 0), 0, 0, 0L, 0, 0);
        return CompletableFuture.completedFuture(new SendResult<>(record, meta));
    }

    private static CompletableFuture<SendResult<String, String>> failedFuture(Throwable t) {
        CompletableFuture<SendResult<String, String>> f = new CompletableFuture<>();
        f.completeExceptionally(t);
        return f;
    }

    /**
     * Never completes — simulates a Kafka broker that's wedged. The publisher
     * must time out via {@code future.get(timeout, ...)} instead of blocking
     * the scheduler thread forever.
     */
    private static CompletableFuture<SendResult<String, String>> hangForever() {
        return new CompletableFuture<>();   // never .complete() / .completeExceptionally()
    }

    /**
     * Spring config to provide just the JPA + Kafka beans we need —
     * {@code @DataJpaTest} only auto-configures JPA, so we bring our own.
     */
    @TestConfiguration
    static class NoOpKafkaConfig {
        // No actual KafkaTemplate bean — the publisher under test takes one
        // as a constructor arg directly (we mock it in @BeforeEach).
    }
}
