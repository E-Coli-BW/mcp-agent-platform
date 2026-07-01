package com.example.modelrouter.service;

import com.example.modelrouter.provider.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for ModelRouterService — tests routing logic with mock providers.
 */
class ModelRouterServiceTest {

    private ModelRouterService router;
    private TestProvider providerA;
    private TestProvider providerB;

    @BeforeEach
    void setUp() {
        providerA = new TestProvider("alpha", true, "response from alpha");
        providerB = new TestProvider("beta", true, "response from beta");
        router = new ModelRouterService(List.of(providerA, providerB), "alpha");
    }

    @Test
    void complete_usesDefaultProvider() {
        LlmResponse r = router.complete(LlmRequest.of("hello"), null);
        assertEquals("alpha", r.provider());
        assertTrue(r.content().contains("alpha"));
    }

    @Test
    void complete_usesPreferredProvider() {
        LlmResponse r = router.complete(LlmRequest.of("hello"), "beta");
        assertEquals("beta", r.provider());
    }

    @Test
    void complete_fallsBackWhenPreferredUnavailable() {
        providerB.setAvailable(false);
        LlmResponse r = router.complete(LlmRequest.of("hello"), "beta");
        assertEquals("alpha", r.provider()); // fell back to default
    }

    @Test
    void complete_fallsBackWhenDefaultUnavailable() {
        providerA.setAvailable(false);
        LlmResponse r = router.complete(LlmRequest.of("hello"), null);
        assertEquals("beta", r.provider()); // fell back to any available
    }

    @Test
    void complete_allUnavailable_returnsError() {
        providerA.setAvailable(false);
        providerB.setAvailable(false);
        LlmResponse r = router.complete(LlmRequest.of("hello"), null);
        assertTrue(r.content().contains("No LLM providers available"));
    }

    @Test
    void listModels_showsAllProviders() {
        var models = router.listModels();
        assertEquals(2, models.size());
    }

    @Test
    void listModels_showsAvailability() {
        providerA.setAvailable(false);
        var models = router.listModels();
        var alpha = models.stream().filter(m -> "alpha".equals(m.get("provider"))).findFirst().orElseThrow();
        assertEquals(false, alpha.get("available"));
    }

    // ── Test provider ────────────────────────────────────────────

    static class TestProvider implements LlmProvider {
        private final String providerName;
        private boolean available;
        private final String response;

        TestProvider(String name, boolean available, String response) {
            this.providerName = name;
            this.available = available;
            this.response = response;
        }

        void setAvailable(boolean available) { this.available = available; }

        @Override public String name() { return providerName; }
        @Override public boolean isAvailable() { return available; }

        @Override
        public LlmResponse complete(LlmRequest request) {
            return LlmResponse.of(response, "test-model", providerName, 10, 20, 50);
        }
    }
}
