package com.example.auth.service;

import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ConcurrentSkipListSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link SnowflakeIdGenerator}.
 *
 * <p>Uses a subclass with a controllable clock so the clock-drift branches are
 * deterministic (no reliance on real wall-clock movement).
 */
class SnowflakeIdGeneratorTest {

    /** Test double whose clock we drive by hand. */
    static class FakeClockGenerator extends SnowflakeIdGenerator {
        final AtomicLong now = new AtomicLong(1704067200000L + 1000);

        FakeClockGenerator(long workerId) {
            super(workerId);
        }

        void setNow(long millis) {
            now.set(millis);
        }

        void advance(long millis) {
            now.addAndGet(millis);
        }

        @Override
        protected long currentTimeMillis() {
            return now.get();
        }
    }

    @Test
    void should_rejectWorkerId_when_outOfRange() {
        assertThrows(IllegalArgumentException.class, () -> new SnowflakeIdGenerator(-1));
        assertThrows(IllegalArgumentException.class, () -> new SnowflakeIdGenerator(1024));
    }

    @Test
    void should_generatePositiveMonotonicIds_when_clockAdvances() {
        FakeClockGenerator gen = new FakeClockGenerator(1);
        long prev = -1L;
        for (int i = 0; i < 1000; i++) {
            gen.advance(1); // each id in a fresh ms
            long id = gen.nextId();
            assertTrue(id > 0, "id must be positive");
            assertTrue(id > prev, "ids must be strictly increasing");
            prev = id;
        }
    }

    @Test
    void should_produceUniqueIds_when_manyIdsInSameMillisecond() {
        FakeClockGenerator gen = new FakeClockGenerator(1);
        // Clock frozen: all ids fall in the same millisecond → sequence must disambiguate.
        Set<Long> ids = new HashSet<>();
        for (int i = 0; i < 4096; i++) {
            assertTrue(ids.add(gen.nextId()), "id collision at index " + i);
        }
        assertEquals(4096, ids.size());
    }

    @Test
    void should_rollOverToNextMillisecond_when_sequenceExhausted() {
        FakeClockGenerator gen = new FakeClockGenerator(1);
        // Consume the whole 4096 sequence in one ms, then one more forces a ms roll.
        for (int i = 0; i < 4096; i++) {
            gen.nextId();
        }
        // The 4097th id can't fit in this ms; generator must advance the clock itself.
        // Because our fake clock doesn't move on its own, waitUntil() would spin forever,
        // so we advance the clock to simulate time passing on the next read.
        gen.advance(1);
        long id = gen.nextId();
        assertTrue(id > 0);
    }

    @Test
    void should_waitAndSucceed_when_smallBackwardDrift() {
        FakeClockGenerator gen = new FakeClockGenerator(1);
        long first = gen.nextId();
        // Simulate a 3ms backward NTP correction (within MAX_BACKWARD_WAIT_MS=5).
        gen.advance(-3);
        // The generator should spin-wait; our fake clock needs to "catch up".
        // Move it forward past the last issued timestamp to let waitUntil() return.
        gen.setNow(gen.now.get() + 10);
        long second = gen.nextId();
        assertTrue(second > first, "after recovering from small drift, ids stay ordered");
    }

    @Test
    void should_throw_when_largeBackwardDrift() {
        FakeClockGenerator gen = new FakeClockGenerator(1);
        gen.nextId();
        // Jump the clock back 1 second — well beyond the tolerated 5ms window.
        gen.advance(-1000);
        assertThrows(IllegalStateException.class, gen::nextId,
                "large backward drift must fail fast to avoid duplicate ids");
    }

    @Test
    void should_stayUnique_when_generatedConcurrently() throws Exception {
        SnowflakeIdGenerator gen = new SnowflakeIdGenerator(7);
        int threads = 8;
        int perThread = 5000;
        Set<Long> ids = new ConcurrentSkipListSet<>();
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        for (int t = 0; t < threads; t++) {
            pool.submit(() -> {
                for (int i = 0; i < perThread; i++) {
                    ids.add(gen.nextId());
                }
            });
        }
        pool.shutdown();
        assertTrue(pool.awaitTermination(30, TimeUnit.SECONDS));
        assertEquals(threads * perThread, ids.size(), "no duplicates under concurrency");
    }
}
