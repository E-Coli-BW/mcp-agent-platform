package com.example.agent;

import com.example.agent.agent.FleetBus;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import reactor.test.StepVerifier;

import java.time.Duration;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Tests for the FleetBus — per-root-session pub/sub for subagent events.
 */
class FleetBusTest {

    private FleetBus bus;

    @BeforeEach
    void setUp() {
        bus = new FleetBus();
    }

    @Test
    void should_dropEvent_when_sessionNotRegistered() {
        // Publishing to an unknown session must be a silent no-op
        bus.publishEvent("ghost-session", "c1", "r", "child_start", Map.of("depth", 1));
        assertThat(bus.sessionCount()).isZero();
    }

    @Test
    void should_deliverEvent_when_subscriberPresent() {
        bus.registerSession("s1");

        var flux = bus.subscribe("s1");

        // Publish after subscribing
        bus.publishEvent("s1", "c1", "reader", "child_token", Map.of("token", "hello"));
        bus.publishEvent("s1", "c1", "reader", "child_end",
                Map.of("answer_preview", "hello", "tokens", 2));

        StepVerifier.create(flux.take(2))
                .assertNext(event -> {
                    assertThat(event.get("type")).isEqualTo("child_token");
                    assertThat(event.get("token")).isEqualTo("hello");
                    assertThat(event.get("root_session_id")).isEqualTo("s1");
                })
                .assertNext(event -> {
                    assertThat(event.get("type")).isEqualTo("child_end");
                })
                .verifyComplete();
    }

    @Test
    void should_isolateEvents_when_multipleSessions() {
        bus.registerSession("sA");
        bus.registerSession("sB");

        var fluxA = bus.subscribe("sA").take(1);
        var fluxB = bus.subscribe("sB").take(1);

        bus.publishEvent("sA", "ca", "r", "child_token", Map.of("token", "A1"));
        bus.publishEvent("sB", "cb", "r", "child_token", Map.of("token", "B1"));

        StepVerifier.create(fluxA)
                .assertNext(event -> {
                    assertThat(event.get("root_session_id")).isEqualTo("sA");
                    assertThat(event.get("token")).isEqualTo("A1");
                })
                .verifyComplete();

        StepVerifier.create(fluxB)
                .assertNext(event -> {
                    assertThat(event.get("root_session_id")).isEqualTo("sB");
                    assertThat(event.get("token")).isEqualTo("B1");
                })
                .verifyComplete();
    }

    @Test
    void should_endSubscription_when_sessionUnregistered() {
        bus.registerSession("s1");
        var flux = bus.subscribe("s1");

        // Unregister should complete the subscription
        bus.unregisterSession("s1");

        StepVerifier.create(flux)
                .expectComplete()
                .verify(Duration.ofSeconds(2));

        assertThat(bus.sessionCount()).isZero();
    }

    @Test
    void should_recordCancel_when_requested() {
        bus.registerSession("s1");
        assertThat(bus.isCancelled("s1", "c1")).isFalse();

        assertThat(bus.requestCancel("s1", "c1")).isTrue();
        assertThat(bus.isCancelled("s1", "c1")).isTrue();

        // Unknown child is not cancelled
        assertThat(bus.isCancelled("s1", "c2")).isFalse();
    }

    @Test
    void should_returnFalse_when_cancelOnUnknownSession() {
        assertThat(bus.requestCancel("ghost", "c1")).isFalse();
    }

    @Test
    void should_cleanState_when_resetForTests() {
        bus.registerSession("s1");
        bus.registerSession("s2");
        assertThat(bus.sessionCount()).isEqualTo(2);

        bus.resetForTests();
        assertThat(bus.sessionCount()).isZero();
    }
}

