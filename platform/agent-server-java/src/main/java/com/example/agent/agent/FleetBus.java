package com.example.agent.agent;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * FleetBus — per-root-session in-memory pub/sub for subagent fleet events.
 *
 * <p>When a parent agent calls {@code spawn_subagent}, the parent's ReAct loop is
 * blocked inside the tool call until the child returns. From the SSE stream's
 * perspective, {@code spawn_subagent} is opaque — no progress visible.</p>
 *
 * <p>The FleetBus makes child agent progress visible:
 * <ul>
 *   <li>{@code spawn_subagent} publishes child_start, child_token, child_tool_*,
 *       child_end events to the bus, scoped to the root session.</li>
 *   <li>The SSE stream subscribes to the bus and forwards events to the browser.</li>
 *   <li>A cancel endpoint flips a per-child flag; the child polls it between
 *       tool rounds and exits cooperatively.</li>
 * </ul>
 *
 * <p>Implementation uses Project Reactor {@link Sinks} — native reactive pub/sub
 * with backpressure support. Each subscriber gets its own unicast sink so a slow
 * consumer doesn't starve others.</p>
 */
@Component
public class FleetBus {

    private static final Logger log = LoggerFactory.getLogger(FleetBus.class);

    private final ConcurrentHashMap<String, SessionState> sessions = new ConcurrentHashMap<>();

    /**
     * Register a session for fleet event publishing.
     * Idempotent — calling twice is a no-op.
     */
    public void registerSession(String rootSessionId) {
        sessions.computeIfAbsent(rootSessionId, k -> {
            log.debug("🚌 fleet_bus: registered session={}", rootSessionId);
            return new SessionState();
        });
    }

    /**
     * Unregister a session and terminate all subscribers.
     * Idempotent — safe to call even if not registered.
     */
    public void unregisterSession(String rootSessionId) {
        SessionState state = sessions.remove(rootSessionId);
        if (state != null) {
            state.terminateAll();
            log.debug("🚌 fleet_bus: unregistered session={}", rootSessionId);
        }
    }

    /**
     * Publish an event to all subscribers on the given root session.
     *
     * <p>Non-blocking: if no subscribers are registered, the event is silently dropped.
     * This lets spawn_subagent work without a bus (unit tests, eval harness).</p>
     */
    public void publishEvent(String rootSessionId, Map<String, Object> event) {
        SessionState state = sessions.get(rootSessionId);
        if (state == null) {
            return; // not registered — silently drop
        }
        state.publish(event);
    }

    /**
     * Convenience method to publish a typed fleet event.
     */
    public void publishEvent(String rootSessionId, String childSessionId, String role,
                             String eventType, Map<String, Object> payload) {
        Map<String, Object> event = new ConcurrentHashMap<>(payload);
        event.put("type", eventType);
        event.put("root_session_id", rootSessionId);
        event.put("child_session_id", childSessionId);
        event.put("role", role);
        publishEvent(rootSessionId, event);
    }

    /**
     * Subscribe to fleet events for a session.
     *
     * @return Flux that emits events until the session is unregistered
     */
    public Flux<Map<String, Object>> subscribe(String rootSessionId) {
        registerSession(rootSessionId);
        SessionState state = sessions.get(rootSessionId);
        if (state == null) {
            return Flux.empty();
        }
        return state.addSubscriber();
    }

    /**
     * Request cooperative cancellation of a child agent.
     *
     * @return true if the cancel was recorded, false if session not found
     */
    public boolean requestCancel(String rootSessionId, String childSessionId) {
        SessionState state = sessions.get(rootSessionId);
        if (state == null) {
            return false;
        }
        state.markCancelled(childSessionId);
        log.info("🛑 fleet_bus: cancel requested for child={} in session={}",
                childSessionId, rootSessionId);
        return true;
    }

    /**
     * Check if a child has been cancelled.
     */
    public boolean isCancelled(String rootSessionId, String childSessionId) {
        SessionState state = sessions.get(rootSessionId);
        if (state == null) {
            return false;
        }
        return state.isCancelled(childSessionId);
    }

    /**
     * Number of currently-registered sessions (for tests).
     */
    public int sessionCount() {
        return sessions.size();
    }

    /**
     * Reset all state (for tests).
     */
    public void resetForTests() {
        sessions.values().forEach(SessionState::terminateAll);
        sessions.clear();
    }

    // ── Internal per-session state ────────────────────────────────────────────

    private static class SessionState {
        private final List<Sinks.Many<Map<String, Object>>> subscribers = new CopyOnWriteArrayList<>();
        private final ConcurrentHashMap<String, AtomicBoolean> cancelFlags = new ConcurrentHashMap<>();

        void publish(Map<String, Object> event) {
            for (Sinks.Many<Map<String, Object>> sink : subscribers) {
                Sinks.EmitResult result = sink.tryEmitNext(event);
                if (result.isFailure()) {
                    log.debug("🚌 fleet_bus: event dropped (sink full or cancelled)");
                }
            }
        }

        Flux<Map<String, Object>> addSubscriber() {
            Sinks.Many<Map<String, Object>> sink = Sinks.many().multicast().onBackpressureBuffer(256);
            subscribers.add(sink);
            return sink.asFlux()
                    .doFinally(signal -> subscribers.remove(sink));
        }

        void terminateAll() {
            for (Sinks.Many<Map<String, Object>> sink : subscribers) {
                sink.tryEmitComplete();
            }
            subscribers.clear();
        }

        void markCancelled(String childSessionId) {
            cancelFlags.computeIfAbsent(childSessionId, k -> new AtomicBoolean(false)).set(true);
        }

        boolean isCancelled(String childSessionId) {
            AtomicBoolean flag = cancelFlags.get(childSessionId);
            return flag != null && flag.get();
        }
    }
}

