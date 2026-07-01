package com.example.completion.fim;

/**
 * Qwen2.5-Coder / CodeQwen FIM format.
 * Uses <|fim_prefix|>, <|fim_suffix|>, <|fim_middle|> tokens.
 */
public class QwenFimFormatter implements FimFormatter {

    @Override
    public String format(String prefix, String suffix) {
        return "<|fim_prefix|>" + prefix + "<|fim_suffix|>" + suffix + "<|fim_middle|>";
    }

    @Override
    public String modelFamily() {
        return "qwen";
    }
}
