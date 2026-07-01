package com.example.modelrouter.provider;

public record LlmResponse(
        String content,
        String model,
        String provider,
        int promptTokens,
        int completionTokens,
        long durationMs,
        boolean fromCache
) {
    public int totalTokens() { return promptTokens + completionTokens; }

    public static LlmResponse of(String content, String model, String provider,
                                  int promptTokens, int completionTokens, long durationMs) {
        return new LlmResponse(content, model, provider, promptTokens, completionTokens, durationMs, false);
    }

    public static LlmResponse error(String message, String provider) {
        return new LlmResponse(message, "none", provider, 0, 0, 0, false);
    }

    public static LlmResponse cached(String content, String model, String provider) {
        return new LlmResponse(content, model, provider, 0, 0, 0, true);
    }
}
