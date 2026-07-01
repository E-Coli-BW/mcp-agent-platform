package com.example.completion.fim;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class FimFormatterTest {

    @Test
    void qwenFormat() {
        var fmt = new QwenFimFormatter();
        String result = fmt.format("def hello():\n    ", "\n    return 'world'");
        assertTrue(result.startsWith("<|fim_prefix|>"));
        assertTrue(result.contains("<|fim_suffix|>"));
        assertTrue(result.endsWith("<|fim_middle|>"));
        assertEquals("qwen", fmt.modelFamily());
    }

    @Test
    void deepseekFormat() {
        var fmt = new DeepSeekFimFormatter();
        String result = fmt.format("prefix", "suffix");
        assertEquals("<|fim_begin|>prefix<|fim_hole|>suffix<|fim_end|>", result);
        assertEquals("deepseek", fmt.modelFamily());
    }

    @Test
    void codeLlamaFormat() {
        var fmt = new CodeLlamaFimFormatter();
        String result = fmt.format("prefix", "suffix");
        assertEquals("<PRE> prefix <SUF>suffix <MID>", result);
        assertEquals("codellama", fmt.modelFamily());
    }

    @Test
    void registrySelectsCorrectFormatter() {
        var registry = new FimFormatterRegistry();
        assertTrue(registry.getFormatter("qwen2.5-coder:7b") instanceof QwenFimFormatter);
        assertTrue(registry.getFormatter("deepseek-coder:6.7b") instanceof DeepSeekFimFormatter);
        assertTrue(registry.getFormatter("codellama:13b") instanceof CodeLlamaFimFormatter);
        // Unknown model → default (Qwen)
        assertTrue(registry.getFormatter("unknown-model") instanceof QwenFimFormatter);
        assertTrue(registry.getFormatter(null) instanceof QwenFimFormatter);
    }
}
