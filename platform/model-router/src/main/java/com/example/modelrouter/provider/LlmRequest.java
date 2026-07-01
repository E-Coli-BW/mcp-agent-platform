package com.example.modelrouter.provider;

import java.util.List;

public record LlmRequest(
        String prompt,
        String systemPrompt,
        String model,          // null = use provider default
        Integer maxTokens,     // null = use config default
        Double temperature,    // null = 0.7
        List<Message> messages // null = use prompt as single user message
) {
    public record Message(String role, String content) {}

    /** Simple single-prompt request */
    public static LlmRequest of(String prompt) {
        return new LlmRequest(prompt, null, null, null, null, null);
    }

    public static LlmRequest of(String prompt, String model, Integer maxTokens) {
        return new LlmRequest(prompt, null, model, maxTokens, null, null);
    }
}
