package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Circuit-breaker wrapper around any MemoryStorageBackend.
 *
 * States: CLOSED (normal) → OPEN (failing, reject fast) → HALF_OPEN (probe)
 *
 * When the delegate backend fails repeatedly, the circuit opens and
 * operations fail fast without hitting the backend, reducing cascading failures.
 * After a cool-down period, a single probe request is allowed through.
 *
 * This is a lightweight, dependency-free implementation.
 * For production at scale, use Resilience4j or Sentinel instead.
 */
public class CircuitBreakerBackend implements MemoryStorageBackend {

    private static final Logger log = LoggerFactory.getLogger(CircuitBreakerBackend.class);

    public enum State { CLOSED, OPEN, HALF_OPEN }

    private final MemoryStorageBackend delegate;
    private final int failureThreshold;
    private final long cooldownMillis;

    private volatile State state = State.CLOSED;
    private final AtomicInteger failureCount = new AtomicInteger(0);
    private final AtomicLong lastFailureTime = new AtomicLong(0);

    public CircuitBreakerBackend(MemoryStorageBackend delegate, int failureThreshold, long cooldownMillis) {
        this.delegate = delegate;
        this.failureThreshold = failureThreshold;
        this.cooldownMillis = cooldownMillis;
    }

    public State getState() { return state; }

    @Override
    public void save(String tenant, String key, String value) {
        checkState();
        try {
            delegate.save(tenant, key, value);
            onSuccess();
        } catch (Exception e) {
            onFailure(e);
            throw e;
        }
    }

    @Override
    public String load(String tenant, String key) {
        checkState();
        try {
            String result = delegate.load(tenant, key);
            onSuccess();
            return result;
        } catch (Exception e) {
            onFailure(e);
            throw e;
        }
    }

    @Override
    public boolean delete(String tenant, String key) {
        checkState();
        try {
            boolean result = delegate.delete(tenant, key);
            onSuccess();
            return result;
        } catch (Exception e) {
            onFailure(e);
            throw e;
        }
    }

    @Override
    public List<String> list(String tenant) {
        checkState();
        try {
            List<String> result = delegate.list(tenant);
            onSuccess();
            return result;
        } catch (Exception e) {
            onFailure(e);
            throw e;
        }
    }

    @Override
    public List<String> search(String tenant, String query) {
        checkState();
        try {
            List<String> result = delegate.search(tenant, query);
            onSuccess();
            return result;
        } catch (Exception e) {
            onFailure(e);
            throw e;
        }
    }

    private void checkState() {
        if (state == State.OPEN) {
            if (System.currentTimeMillis() - lastFailureTime.get() > cooldownMillis) {
                state = State.HALF_OPEN;
                log.info("Circuit breaker → HALF_OPEN (probing)");
            } else {
                throw new CircuitBreakerOpenException("Circuit breaker is OPEN — backend unavailable");
            }
        }
    }

    private void onSuccess() {
        if (state != State.CLOSED) {
            log.info("Circuit breaker → CLOSED (backend recovered)");
        }
        state = State.CLOSED;
        failureCount.set(0);
    }

    private void onFailure(Exception e) {
        lastFailureTime.set(System.currentTimeMillis());
        int count = failureCount.incrementAndGet();
        log.warn("Backend failure #{}: {}", count, e.getMessage());
        if (count >= failureThreshold) {
            state = State.OPEN;
            log.error("Circuit breaker → OPEN after {} failures", count);
        }
    }

    public static class CircuitBreakerOpenException extends RuntimeException {
        public CircuitBreakerOpenException(String message) { super(message); }
    }
}
