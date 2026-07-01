package com.example.agent.governance;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Validates model-call provenance payloads against the shared JSON schema.
 */
@Component
public class ModelCallProvenanceSchemaValidator {

    private static final Logger log = LoggerFactory.getLogger(ModelCallProvenanceSchemaValidator.class);
    private static final ObjectMapper MAPPER = new ObjectMapper().findAndRegisterModules();
    private static final List<Path> SCHEMA_CANDIDATES = List.of(
            Path.of("platform", "docs", "design", "schemas", "model-call-provenance.schema.json"),
            Path.of("..", "docs", "design", "schemas", "model-call-provenance.schema.json"),
            Path.of("docs", "design", "schemas", "model-call-provenance.schema.json")
    );
    private static final String CLASSPATH_SCHEMA = "/schemas/model-call-provenance.schema.json";

    private final JsonSchema schema;

    public ModelCallProvenanceSchemaValidator() {
        this.schema = loadSchema();
    }

    public ValidationResult validate(ModelCallProvenance event) {
        return validatePayload(toSchemaPayload(event));
    }

    public ValidationResult validatePayload(Map<String, Object> payload) {
        if (schema == null) {
            // Keep provenance non-blocking if schema is unavailable.
            return ValidationResult.success();
        }
        try {
            JsonNode payloadNode = MAPPER.valueToTree(payload);
            Set<ValidationMessage> errors = schema.validate(payloadNode);
            if (errors.isEmpty()) {
                return ValidationResult.success();
            }
            ValidationMessage first = errors.iterator().next();
            String location = first.getInstanceLocation() != null
                    ? first.getInstanceLocation().toString()
                    : "<root>";
            return ValidationResult.failure(location + ": " + first.getMessage());
        } catch (Exception e) {
            return ValidationResult.failure("validation_error: " + e.getMessage());
        }
    }

    public Map<String, Object> toSchemaPayload(ModelCallProvenance event) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("event_id", event.eventId());
        payload.put("run_id", event.runId());
        payload.put("request_id", event.requestId());
        payload.put("trace_id", event.traceId());
        payload.put("tenant_id", event.tenantId());
        payload.put("session_id", event.sessionId());
        payload.put("runtime", event.runtime());
        payload.put("call_site", event.callSite());
        payload.put("provider", event.provider());
        payload.put("model", event.model());
        payload.put("temperature", event.temperature());
        payload.put("max_tokens", event.maxTokens());
        payload.put("prompt_id", event.promptId());
        payload.put("prompt_version", event.promptVersion());
        payload.put("prompt_hash", event.promptHash());
        payload.put("feature_flags", event.featureFlags());
        payload.put("prompt_tokens", event.promptTokens());
        payload.put("completion_tokens", event.completionTokens());
        payload.put("duration_ms", event.durationMs());
        payload.put("fallback_count", event.fallbackCount());
        payload.put("retry_count", event.retryCount());
        payload.put("status", event.status());
        payload.put("error_class", event.errorClass());
        payload.put("timestamp", event.timestamp() != null ? event.timestamp().toString() : null);
        return payload;
    }

    private JsonSchema loadSchema() {
        JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);

        for (Path relative : SCHEMA_CANDIDATES) {
            Path resolved = relative.toAbsolutePath().normalize();
            if (!Files.exists(resolved)) {
                continue;
            }
            try (InputStream in = Files.newInputStream(resolved)) {
                JsonNode schemaNode = MAPPER.readTree(in);
                log.info("Loaded model-call provenance schema from {}", resolved);
                return factory.getSchema(schemaNode);
            } catch (Exception e) {
                log.warn("Failed to load provenance schema from {}: {}", resolved, e.getMessage());
            }
        }

        try (InputStream in = ModelCallProvenanceSchemaValidator.class.getResourceAsStream(CLASSPATH_SCHEMA)) {
            if (in != null) {
                JsonNode schemaNode = MAPPER.readTree(in);
                log.info("Loaded model-call provenance schema from classpath {}", CLASSPATH_SCHEMA);
                return factory.getSchema(schemaNode);
            }
        } catch (Exception e) {
            log.warn("Failed to load classpath provenance schema {}: {}", CLASSPATH_SCHEMA, e.getMessage());
        }

        log.warn("Model-call provenance schema unavailable; validation disabled");
        return null;
    }

    public record ValidationResult(boolean valid, String error) {
        public static ValidationResult success() {
            return new ValidationResult(true, null);
        }

        public static ValidationResult failure(String error) {
            return new ValidationResult(false, error);
        }
    }
}



