package com.example.agent.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.ArrayList;
import java.util.List;

@JsonIgnoreProperties(ignoreUnknown = true)
public record ChatRequest(
        String model,
        List<ChatMessage> messages,
        Boolean stream,
        Double temperature,
        @JsonProperty("max_tokens") Integer maxTokens,
        @JsonProperty("prompt_version") String promptVersion,
        @JsonProperty("session_id") String sessionId,
        @JsonProperty("active_file") ActiveFileContext activeFile
) {

    public ChatRequest {
        model = model == null || model.isBlank() ? "coding-agent" : model;
        messages = messages == null ? List.of() : List.copyOf(messages);
        stream = stream == null ? Boolean.TRUE : stream;
    }

    public String lastUserMessage() {
        if (messages.isEmpty()) {
            return "";
        }
        ChatMessage last = messages.get(messages.size() - 1);
        return last == null || last.content() == null ? "" : last.content();
    }

    public ChatRequest withLastUserMessage(String content) {
        if (messages.isEmpty()) {
            return this;
        }
        List<ChatMessage> updated = new ArrayList<>(messages);
        ChatMessage last = updated.get(updated.size() - 1);
        updated.set(updated.size() - 1, new ChatMessage(last.role(), content));
        return new ChatRequest(model, updated, stream, temperature, maxTokens, promptVersion, sessionId, activeFile);
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ChatMessage(String role, String content) {
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ActiveFileContext(
            String path,
            @JsonProperty("visible_start") Integer visibleStart,
            @JsonProperty("visible_end") Integer visibleEnd
    ) {
    }
}
