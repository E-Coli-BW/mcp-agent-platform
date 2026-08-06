package com.example.auth.service;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * Snowflake distributed ID generator (Twitter Snowflake, Baidu UidGenerator style).
 *
 * <p>A 64-bit long ID laid out as:
 * <pre>
 *   0 | 41-bit timestamp (ms since epoch) | 10-bit worker id | 12-bit sequence
 *   ┬   ┬                                   ┬                  ┬
 *   │   │                                   │                  └ 4096 ids / ms / worker
 *   │   │                                   └ 1024 workers (5-bit datacenter + 5-bit machine)
 *   │   └ ~69 years from a custom epoch
 *   └ sign bit, always 0 → IDs are positive and monotonically increasing
 * </pre>
 *
 * <h3>Why Snowflake</h3>
 * Locally generated (no central bottleneck), roughly time-ordered (good as a DB
 * primary key — avoids the page-split churn random UUIDs cause on B+tree indexes),
 * and compact (fits in a {@code bigint}).
 *
 * <h3>The clock-drift failure mode</h3>
 * Snowflake trusts the wall clock. If NTP or an operator moves the clock
 * <em>backwards</em>, a freshly generated timestamp can be smaller than one we
 * already used → duplicate IDs, which is catastrophic for a primary key. This
 * class handles it defensively:
 * <ul>
 *   <li><b>Small backward drift</b> (≤ {@code MAX_BACKWARD_WAIT_MS}) — busy-wait
 *       until the clock catches up to the last timestamp we issued.</li>
 *   <li><b>Large backward drift</b> — fail fast with an exception rather than
 *       risk duplicates. Ops can then quarantine the node. (Baidu/Leaf go further
 *       and cache the last timestamp in ZK; that's the next step if we needed
 *       cross-restart safety.)</li>
 * </ul>
 *
 * <h3>Concurrency</h3>
 * {@link #nextId()} is {@code synchronized} — the critical section is a handful of
 * arithmetic ops, so lock contention is negligible next to the 4096 ids/ms ceiling.
 * A lock-free CAS version is possible but the added complexity isn't justified here.
 */
@Component
public class SnowflakeIdGenerator {

    /** Custom epoch: 2024-01-01T00:00:00Z. Buys ~69 years of 41-bit timestamps. */
    private static final long EPOCH = 1704067200000L;

    private static final long WORKER_ID_BITS = 10L;
    private static final long SEQUENCE_BITS = 12L;

    private static final long MAX_WORKER_ID = ~(-1L << WORKER_ID_BITS); // 1023
    private static final long SEQUENCE_MASK = ~(-1L << SEQUENCE_BITS);  // 4095

    private static final long WORKER_ID_SHIFT = SEQUENCE_BITS;              // 12
    private static final long TIMESTAMP_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS; // 22

    /** Backward drift we're willing to spin-wait through. Beyond this, we fail. */
    private static final long MAX_BACKWARD_WAIT_MS = 5L;

    private final long workerId;

    private long lastTimestamp = -1L;
    private long sequence = 0L;

    public SnowflakeIdGenerator(@Value("${snowflake.worker-id:1}") long workerId) {
        if (workerId < 0 || workerId > MAX_WORKER_ID) {
            throw new IllegalArgumentException(
                    "workerId must be in [0, " + MAX_WORKER_ID + "], got " + workerId);
        }
        this.workerId = workerId;
    }

    /**
     * Generate the next unique, time-ordered ID.
     *
     * @return a positive 64-bit id
     * @throws IllegalStateException if the clock moved backwards beyond the tolerated window
     */
    public synchronized long nextId() {
        long timestamp = currentTimeMillis();

        if (timestamp < lastTimestamp) {
            long drift = lastTimestamp - timestamp;
            if (drift <= MAX_BACKWARD_WAIT_MS) {
                // Small drift: wait for the clock to catch up, then re-read.
                timestamp = waitUntil(lastTimestamp);
            } else {
                throw new IllegalStateException(
                        "Clock moved backwards by " + drift + "ms — refusing to generate id "
                                + "(risk of duplicates). Quarantine this node.");
            }
        }

        if (timestamp == lastTimestamp) {
            // Same millisecond: bump the sequence.
            sequence = (sequence + 1) & SEQUENCE_MASK;
            if (sequence == 0) {
                // Sequence exhausted for this ms (>4096 ids) — spin to the next ms.
                timestamp = waitUntil(lastTimestamp + 1);
            }
        } else {
            // New millisecond: reset the sequence.
            sequence = 0L;
        }

        lastTimestamp = timestamp;

        return ((timestamp - EPOCH) << TIMESTAMP_SHIFT)
                | (workerId << WORKER_ID_SHIFT)
                | sequence;
    }

    /** Busy-wait until {@link #currentTimeMillis()} reaches {@code target}. */
    private long waitUntil(long target) {
        long ts = currentTimeMillis();
        while (ts < target) {
            ts = currentTimeMillis();
        }
        return ts;
    }

    /** Isolated for tests to inject a controllable clock. */
    protected long currentTimeMillis() {
        return System.currentTimeMillis();
    }
}
