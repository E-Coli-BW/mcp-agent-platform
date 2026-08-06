package com.example.memoryserver.event;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.Map;

/**
 * Schema for tool execution events consumed from Kafka.
 * Shared between Python producer and Java consumer.
 *
 * INTERVIEW POINT: This is a cross-language event schema. The Python
 * agent produces JSON events, the Java consumer deserializes them.
 * In production, you'd use Avro/Protobuf with a schema registry.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record ToolEventRecord(
        @JsonProperty("event_id") String eventId,
        @JsonProperty("timestamp") String timestamp,
        @JsonProperty("session_id") String sessionId,
        @JsonProperty("event_type") String eventType,
        @JsonProperty("tool_name") String toolName,
        @JsonProperty("tool_input") Map<String, Object> toolInput,
        @JsonProperty("tool_output") String toolOutput,
        @JsonProperty("model") String model,
        @JsonProperty("duration_ms") int durationMs,
        @JsonProperty("token_count") int tokenCount
) {}
