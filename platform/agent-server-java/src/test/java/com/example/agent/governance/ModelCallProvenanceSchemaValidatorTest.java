package com.example.agent.governance;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class ModelCallProvenanceSchemaValidatorTest {

    @Test
    void shouldAcceptValidPayload_whenSchemaConstraintsSatisfied() {
        ModelCallProvenanceSchemaValidator validator = new ModelCallProvenanceSchemaValidator();

        ModelCallProvenance event = new ModelCallProvenance(
                "mcall-123",
                "run-1",
                "req-1",
                null,
                "tenant-a",
                "tenant-a:session-1",
                "java-loop",
                "AgentLoopService.runLoop.llm_call_1",
                "ollama",
                "qwen2.5:7b",
                0.7,
                1024,
                "coding-agent.system",
                "v2",
                "sha256:" + "a".repeat(64),
                Map.of(
                        "prompt_version_default", "v2",
                        "max_agent_steps", 20
                ),
                100,
                25,
                400,
                0,
                0,
                "ok",
                null,
                Instant.parse("2026-06-02T00:00:00Z")
        );

        ModelCallProvenanceSchemaValidator.ValidationResult result = validator.validate(event);
        assertThat(result.valid()).isTrue();
        assertThat(result.error()).isNull();
    }

    @Test
    void shouldRejectInvalidPromptHash_whenSchemaValidationRuns() {
        ModelCallProvenanceSchemaValidator validator = new ModelCallProvenanceSchemaValidator();

        ModelCallProvenance event = new ModelCallProvenance(
                "mcall-123",
                "run-1",
                "req-1",
                null,
                "tenant-a",
                "tenant-a:session-1",
                "java-loop",
                "AgentLoopService.runLoop.llm_call_1",
                "ollama",
                "qwen2.5:7b",
                0.7,
                1024,
                "coding-agent.system",
                "v2",
                "sha256:" + "a".repeat(64),
                Map.of("prompt_version_default", "v2"),
                100,
                25,
                400,
                0,
                0,
                "ok",
                null,
                Instant.parse("2026-06-02T00:00:00Z")
        );

        Map<String, Object> payload = validator.toSchemaPayload(event);
        payload.put("prompt_hash", "not-a-sha256");

        ModelCallProvenanceSchemaValidator.ValidationResult result = validator.validatePayload(payload);
        assertThat(result.valid()).isFalse();
        assertThat(result.error()).isNotNull();
        assertThat(result.error()).contains("prompt_hash");
    }
}



