package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

/**
 * Circuit-breaker wrapper around any {@link MemoryStorageBackend}.
 *
 * <p>States: {@code CLOSED} (normal) -> {@code OPEN} (fail fast) ->
 * {@code HALF_OPEN} (single probe) -> back to {@code CLOSED} or {@code OPEN}.
 *
 * <p>Two concurrency properties this implementation must hold, both of which a
 * naive version gets wrong:
 *
 * <ol>
 *   <li><b>State transitions are atomic.</b> A {@code volatile} field only
 *       guarantees visibility, not atomicity. With plain assignment, a thread
 *       in {@code onFailure} that has already crossed the failure threshold can
 *       be interleaved by a thread in {@code onSuccess}, leaving the breaker
 *       {@code OPEN} even though the backend just recovered (or the reverse).
 *       Every transition here goes through {@link AtomicReference#compareAndSet}
 *       so a losing thread observes the winner's state instead of overwriting
 *       it.</li>
 *   <li><b>Only one probe runs in {@code HALF_OPEN}.</b> Without a permit, every
 *       request that arrives after the cool-down expires is admitted at once -
 *       a thundering herd onto the backend that is, by definition, the one we
 *       just decided is unhealthy. A single permit means one caller probes and
 *       the rest keep failing fast.</li>
 * </ol>
 *
 * <p>This is a deliberately small, dependency-free implementation used by the
 * storage SPI. Service-to-service calls use Resilience4j; a library is the
 * right default whenever the policy needs metrics, sliding windows, or
 * bulkheads on top of the state machine.
 */
public class CircuitBreakerBackend implements MemoryStorageBackend {

    private static final Logger log = LoggerFactory.getLogger(CircuitBreakerBackend.class);

    public enum State { CLOSED, OPEN, HALF_OPEN }

    private final MemoryStorageBackend delegate;
    private final int failureThreshold;
    private final long cooldownMillis;

    private final AtomicReference<State> state = new AtomicReference<>(State.CLOSED);
    private final AtomicInteger failureCount = new AtomicInteger(0);
    private final AtomicLong lastFailureTime = new AtomicLong(0);

    /** One in-flight probe at a time while HALF_OPEN. */
    private final Semaphore probePermit = new Semaphore(1);

    public CircuitBreakerBackend(MemoryStorageBackend delegate, int failureThreshold, long cooldownMillis) {
        this.delegate = delegate;
        this.failureThreshold = failureThreshold;
        this.cooldownMillis = cooldownMillis;
    }

    public State getState() {
        return state.get();
    }

    @Override
    public void save(String tenant, String key, String value) {
        call(() -> {
            delegate.save(tenant, key, value);
            return null;
        });
    }

    @Override
    public String load(String tenant, String key) {
        return call(() -> delegate.load(tenant, key));
    }

    @Override
    public boolean delete(String tenant, String key) {
        return call(() -> delegate.delete(tenant, key));
    }

    @Override
    public List<String> list(String tenant) {
        return call(() -> delegate.list(tenant));
    }

    @Override
    public List<String> search(String tenant, String query) {
        return call(() -> delegate.search(tenant, query));
    }

    /**
     * Single guarded call path, so admission control and the success/failure
     * bookkeeping cannot drift apart between operations.
     */
    private <T> T call(Supplier<T> operation) {
        boolean probing = acquireAdmission();
        try {
            T result = operation.get();
            onSuccess();
            return result;
        } catch (Exception e) {
            onFailure(e);
            throw e;
        } finally {
            if (probing) {
                probePermit.release();
            }
        }
    }

    /**
     * Decides whether this caller may reach the delegate.
     *
     * @return {@code true} if the caller holds the HALF_OPEN probe permit and
     *         must release it once the call completes.
     * @throws CircuitBreakerOpenException if the circuit rejects the call.
     */
    private boolean acquireAdmission() {
        while (true) {
            State current = state.get();

            if (current == State.CLOSED) {
                return false;
            }

            if (current == State.OPEN) {
                boolean cooledDown = System.currentTimeMillis() - lastFailureTime.get() > cooldownMillis;
                if (!cooledDown) {
                    throw new CircuitBreakerOpenException("Circuit breaker is OPEN - backend unavailable");
                }
                // Losing the CAS is fine: another thread already moved us on,
                // so re-read the state instead of assuming the transition.
                if (state.compareAndSet(State.OPEN, State.HALF_OPEN)) {
                    log.info("Circuit breaker -> HALF_OPEN (probing)");
                }
                continue;
            }

            // HALF_OPEN: exactly one probe is admitted; everyone else fails fast
            // rather than stampeding a backend we already believe is unhealthy.
            if (!probePermit.tryAcquire()) {
                throw new CircuitBreakerOpenException("Circuit breaker is HALF_OPEN - probe in progress");
            }
            if (state.get() != State.HALF_OPEN) {
                // Raced with a concurrent transition; retry under the new state.
                probePermit.release();
                continue;
            }
            return true;
        }
    }

    private void onSuccess() {
        failureCount.set(0);
        State previous = state.getAndSet(State.CLOSED);
        if (previous != State.CLOSED) {
            log.info("Circuit breaker -> CLOSED (backend recovered)");
        }
    }

    private void onFailure(Exception e) {
        lastFailureTime.set(System.currentTimeMillis());
        int count = failureCount.incrementAndGet();
        log.warn("Backend failure #{}: {}", count, e.getMessage());

        // A failed probe sends us straight back to OPEN, restarting the
        // cool-down; there is no reason to spend the rest of the threshold
        // budget on a backend that just failed its health check.
        if (state.compareAndSet(State.HALF_OPEN, State.OPEN)) {
            log.error("Circuit breaker -> OPEN (probe failed)");
            return;
        }

        if (count >= failureThreshold && state.compareAndSet(State.CLOSED, State.OPEN)) {
            log.error("Circuit breaker -> OPEN after {} failures", count);
        }
    }

    public static class CircuitBreakerOpenException extends RuntimeException {
        public CircuitBreakerOpenException(String message) { super(message); }
    }
}
