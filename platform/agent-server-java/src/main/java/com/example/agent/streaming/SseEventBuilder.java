package com.example.agent.streaming;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.http.codec.ServerSentEvent;

import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * Helper for building SSE events compatible with the Python frontend.
 */
public final class SseEventBuilder {

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private SseEventBuilder() {
    }

    /**
     * Create a tool_start event.
     *
     * @param tool tool name
     * @param input tool input
     * @param seq sequence number
     * @return SSE event
     */
    public static ServerSentEvent<String> toolStart(String tool, Map<String, Object> input, int seq) {
        Map<String, Object> data = new HashMap<>();
        data.put("tool", tool);
        data.put("input", input);
        data.put("seq", seq);
        return ServerSentEvent.<String>builder().event("tool_start").data(toJson(data)).build();
    }

    /**
     * Create a tool_end event.
     *
     * @param tool tool name
     * @param input tool input
     * @param output tool output
     * @param seq sequence number
     * @return SSE event
     */
    public static ServerSentEvent<String> toolEnd(String tool, Map<String, Object> input, String output, int seq) {
        Map<String, Object> data = new HashMap<>();
        data.put("tool", tool);
        data.put("input", input);
        data.put("output", output != null && output.length() > 200 ? output.substring(0, 200) : output);
        data.put("seq", seq);
        return ServerSentEvent.<String>builder().event("tool_end").data(toJson(data)).build();
    }

    /**
     * Create a status event.
     *
     * @param state status state
     * @param extra extra fields
     * @return SSE event
     */
    public static ServerSentEvent<String> status(String state, Map<String, Object> extra) {
        Map<String, Object> data = new HashMap<>(extra != null ? extra : Map.of());
        data.put("state", state);
        return ServerSentEvent.<String>builder().event("status").data(toJson(data)).build();
    }

    /**
     * Create an OpenAI-compatible content chunk.
     *
     * @param content content chunk
     * @param model model identifier
     * @param finishReason finish reason or null
     * @return SSE event
     */
    public static ServerSentEvent<String> contentChunk(String content, String model, String finishReason) {
        Map<String, Object> choice = new HashMap<>();
        choice.put("index", 0);
        choice.put("delta", content != null ? Map.of("content", content) : Map.of());
        choice.put("finish_reason", finishReason);

        Map<String, Object> chunk = new HashMap<>();
        chunk.put("id", "chatcmpl-" + UUID.randomUUID().toString().substring(0, 8));
        chunk.put("object", "chat.completion.chunk");
        chunk.put("created", Instant.now().getEpochSecond());
        chunk.put("model", model);
        chunk.put("choices", List.of(choice));
        return ServerSentEvent.<String>builder().data(toJson(chunk)).build();
    }

    /**
     * Create the terminal [DONE] frame.
     *
     * @return SSE event
     */
    public static ServerSentEvent<String> done() {
        return ServerSentEvent.<String>builder().data("[DONE]").build();
    }

    private static String toJson(Object obj) {
        try {
            return MAPPER.writeValueAsString(obj);
        }
        catch (JsonProcessingException e) {
            return "{}";
        }
    }
}
