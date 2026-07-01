package com.example.completion.api;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;

/**
 * OpenAI-compatible completion request.
 * Supports two modes:
 * 1. Raw FIM prompt (model-specific tokens in "prompt" field)
 * 2. IDE-friendly (file_content + cursor_line + cursor_column)
 */
public record CompletionRequest(
        String model,
        String prompt,              // Raw FIM prompt (mode 1)
        @JsonProperty("file_content") String fileContent,  // Full file text (mode 2)
        @JsonProperty("cursor_line") Integer cursorLine,    // 0-based (mode 2)
        @JsonProperty("cursor_column") Integer cursorColumn, // 0-based (mode 2)
        String language,
        @JsonProperty("file_path") String filePath,
        @JsonProperty("max_tokens") Integer maxTokens,
        Double temperature,
        boolean stream,
        List<String> stop
) {
    public CompletionRequest {
        if (stream == false && prompt == null && fileContent == null) {
            // default
        }
    }

    /** Is this a raw FIM prompt or IDE cursor-based? */
    public boolean isRawPrompt() {
        return prompt != null && !prompt.isBlank();
    }
}
