package com.example.completion.fim;

import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * Registry of FIM formatters.
 * Selects the right formatter based on model name.
 */
@Component
public class FimFormatterRegistry {

    private final Map<String, FimFormatter> formatters;
    private final FimFormatter defaultFormatter;

    public FimFormatterRegistry() {
        List<FimFormatter> all = List.of(
                new QwenFimFormatter(),
                new DeepSeekFimFormatter(),
                new CodeLlamaFimFormatter()
        );
        this.formatters = all.stream()
                .collect(Collectors.toMap(FimFormatter::modelFamily, Function.identity()));
        this.defaultFormatter = new QwenFimFormatter(); // default for Ollama models
    }

    /**
     * Get the FIM formatter for a given model name.
     * Matches by prefix: "qwen2.5-coder:7b" → "qwen", "deepseek-coder" → "deepseek".
     */
    public FimFormatter getFormatter(String modelName) {
        if (modelName == null) return defaultFormatter;
        String lower = modelName.toLowerCase();
        for (var entry : formatters.entrySet()) {
            if (lower.contains(entry.getKey())) {
                return entry.getValue();
            }
        }
        return defaultFormatter;
    }
}
