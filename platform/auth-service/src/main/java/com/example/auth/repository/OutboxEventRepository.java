package com.example.auth.repository;

import com.example.auth.model.OutboxEvent;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface OutboxEventRepository extends JpaRepository<OutboxEvent, String> {

    /**
     * Legacy convenience query — used by tests that just want to read pending
     * events without acquiring locks. The publisher hot path uses
     * {@link #findUnpublishedForUpdateSkipLocked(int)} instead.
     */
    List<OutboxEvent> findByPublishedFalseOrderByCreatedAtAsc(Pageable pageable);

    /**
     * The publisher's main query — claims a batch of unpublished, non-dead
     * events for processing using {@code SELECT ... FOR UPDATE SKIP LOCKED}.
     *
     * <p>Why this exact SQL:
     * <ul>
     *   <li><b>{@code FOR UPDATE}</b> takes a row-level write lock. Without it,
     *       two publisher instances scanning the same window will both see the
     *       same rows and publish them twice (Kafka has no dedup unless the
     *       downstream consumer is idempotent — and many of ours aren't yet).</li>
     *   <li><b>{@code SKIP LOCKED}</b> means "don't block on rows another
     *       transaction has already locked — just skip them." This is what
     *       turns the pessimistic lock into a horizontally-scalable claim
     *       queue: N publisher instances cleanly partition the unpublished
     *       set without coordination.</li>
     *   <li><b>{@code dead = false}</b> filters out poison messages. Without
     *       this, a permanently-failing event would burn the entire batch
     *       budget every second forever.</li>
     *   <li><b>{@code ORDER BY created_at ASC}</b> preserves FIFO ordering
     *       within a tenant key (Kafka's per-partition ordering still holds
     *       at the broker level, this is just so we don't lap ourselves).</li>
     * </ul>
     *
     * <p>H2 supports {@code SKIP LOCKED} only in PostgreSQL compatibility mode
     * ({@code jdbc:h2:...;MODE=PostgreSQL}). Tests that exercise the publisher
     * MUST use that mode, which is also a good thing — it surfaces other
     * Postgres-specific behaviour earlier.
     */
    @Query(value = """
            SELECT * FROM outbox_events
            WHERE published = false AND dead = false
            ORDER BY created_at ASC
            LIMIT :batchSize
            FOR UPDATE SKIP LOCKED
            """, nativeQuery = true)
    List<OutboxEvent> findUnpublishedForUpdateSkipLocked(@Param("batchSize") int batchSize);
}

