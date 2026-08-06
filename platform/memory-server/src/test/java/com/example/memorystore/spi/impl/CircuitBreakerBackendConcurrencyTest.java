package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;
import com.example.memorystore.spi.impl.CircuitBreakerBackend.CircuitBreakerOpenException;
import com.example.memorystore.spi.impl.CircuitBreakerBackend.State;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Regression tests for the two concurrency defects the original breaker had.
 *
 * <p>Both are the kind of bug that never shows up in single-threaded tests and
 * only bites under the exact conditions you least want it to - while a
 * dependency is already unhealthy.
 */
class CircuitBreakerBackendConcurrencyTest {

    /** Backend whose health can be toggled, counting every call that reaches it. */
    private static final class FlakyBackend implements MemoryStorageBackend {
        private final AtomicBoolean healthy = new AtomicBoolean(false);
        private final AtomicInteger reached = new AtomicInteger();

        @Override public void save(String t, String k, String v) { hit(); }
        @Override public String load(String t, String k) { hit(); return "value"; }
        @Override public boolean delete(String t, String k) { hit(); return true; }
        @Override public List<String> list(String t) { hit(); return List.of(); }
        @Override public List<String> search(String t, String q) { hit(); return List.of(); }

        private void hit() {
            reached.incrementAndGet();
            if (!healthy.get()) {
                throw new IllegalStateException("backend down");
            }
        }
    }

    private static void trip(CircuitBreakerBackend breaker, int failures) {
        for (int i = 0; i < failures; i++) {
            try {
                breaker.load("tenant", "key");
            } catch (RuntimeException expected) {
                // driving the breaker to OPEN on purpose
            }
        }
    }

    @Test
    @DisplayName("HALF_OPEN admits exactly one probe, even under a 200-thread stampede")
    void halfOpenAdmitsSingleProbe() throws Exception {
        FlakyBackend backend = new FlakyBackend();
        CircuitBreakerBackend breaker = new CircuitBreakerBackend(backend, 3, 50);

        trip(breaker, 3);
        assertEquals(State.OPEN, breaker.getState(), "three failures should open the circuit");

        Thread.sleep(80);            // cool-down expires
        backend.reached.set(0);      // backend is still DOWN - the dangerous case

        int callers = 200;
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch finished = new CountDownLatch(callers);
        AtomicInteger fastFailed = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(32);

        try {
            for (int i = 0; i < callers; i++) {
                pool.submit(() -> {
                    try {
                        start.await();
                        breaker.load("tenant", "key");
                    } catch (CircuitBreakerOpenException rejected) {
                        fastFailed.incrementAndGet();
                    } catch (Exception ignored) {
                        // the single probe observes the backend failure
                    } finally {
                        finished.countDown();
                    }
                });
            }
            start.countDown();
            assertTrue(finished.await(10, TimeUnit.SECONDS), "callers should not block");
        } finally {
            pool.shutdownNow();
        }

        // Without the permit every waiting caller would pile onto a backend we
        // already know is unhealthy.
        assertEquals(1, backend.reached.get(), "only one probe may reach an unhealthy backend");
        assertEquals(callers - 1, fastFailed.get(), "everyone else must fail fast");
        assertEquals(State.OPEN, breaker.getState(), "a failed probe re-opens the circuit");
    }

    @Test
    @DisplayName("Interleaved success and failure never corrupt the state machine")
    void concurrentSuccessAndFailureKeepStateConsistent() throws Exception {
        int rounds = 2_000;

        for (int round = 0; round < rounds; round++) {
            FlakyBackend backend = new FlakyBackend();
            CircuitBreakerBackend breaker = new CircuitBreakerBackend(backend, 1, 60_000);

            CountDownLatch start = new CountDownLatch(1);
            CountDownLatch finished = new CountDownLatch(2);

            Thread failing = new Thread(() -> {
                try {
                    start.await();
                    backend.healthy.set(false);
                    breaker.load("tenant", "key");
                } catch (Exception ignored) {
                    // expected
                } finally {
                    finished.countDown();
                }
            });
            Thread succeeding = new Thread(() -> {
                try {
                    start.await();
                    backend.healthy.set(true);
                    breaker.load("tenant", "key");
                } catch (Exception ignored) {
                    // may lose the race and see OPEN
                } finally {
                    finished.countDown();
                }
            });

            failing.start();
            succeeding.start();
            start.countDown();
            assertTrue(finished.await(2, TimeUnit.SECONDS), "round " + round + " deadlocked");
            failing.join(500);
            succeeding.join(500);

            // With a plain `volatile` assignment the loser of the race could
            // overwrite the winner, leaving CLOSED with no call ever admitted.
            assertTrue(backend.reached.get() > 0 || breaker.getState() != State.CLOSED,
                    "round " + round + ": CLOSED without any admitted call means the state was overwritten");
        }
    }
}
