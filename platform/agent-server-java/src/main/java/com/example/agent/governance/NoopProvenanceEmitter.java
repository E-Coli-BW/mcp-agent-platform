package com.example.agent.governance;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * Default provenance emitter.
 *
 * <p>Logs events as compact JSON so model-call provenance is visible immediately
 * without requiring Kafka/OTLP wiring. Replace with a transport-backed emitter
 * when central collection is available.</p>
 */
@Component
public class NoopProvenanceEmitter implements ProvenanceEmitter {

    private static final Logger log = LoggerFactory.getLogger(NoopProvenanceEmitter.class);
    private static final ObjectMapper MAPPER = new ObjectMapper().findAndRegisterModules();
    private final ModelCallProvenanceSchemaValidator schemaValidator;

    public NoopProvenanceEmitter(ModelCallProvenanceSchemaValidator schemaValidator) {
        this.schemaValidator = schemaValidator;
    }

    @Override
    public void emitModelCall(ModelCallProvenance event) {
        Map<String, Object> payload = schemaValidator.toSchemaPayload(event);
        ModelCallProvenanceSchemaValidator.ValidationResult validation = schemaValidator.validatePayload(payload);
        if (!validation.valid()) {
            log.warn(
                    "Dropping invalid model-call provenance event [event_id={}]: {}",
                    payload.getOrDefault("event_id", "unknown"),
                    validation.error()
            );
            return;
        }
        try {
            log.info("model_call_provenance {}", MAPPER.writeValueAsString(payload));
        } catch (JsonProcessingException e) {
            log.info("model_call_provenance model={} prompt={} runtime={} status={}",
                    event.model(), event.promptVersion(), event.runtime(), event.status());
        }
    }
}








