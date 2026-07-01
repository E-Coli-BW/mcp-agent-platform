package com.example.completion.fim;

/**
 * DeepSeek Coder FIM format.
 * Uses <|fim_begin|>, <|fim_hole|>, <|fim_end|> tokens.
 */
public class DeepSeekFimFormatter implements FimFormatter {

    @Override
    public String format(String prefix, String suffix) {
        return "<|fim_begin|>" + prefix + "<|fim_hole|>" + suffix + "<|fim_end|>";
    }

    @Override
    public String modelFamily() {
        return "deepseek";
    }
}
