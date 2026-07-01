package com.example.completion.fim;

/**
 * CodeLlama / StarCoder FIM format.
 * Uses <PRE>, <SUF>, <MID> tokens.
 */
public class CodeLlamaFimFormatter implements FimFormatter {

    @Override
    public String format(String prefix, String suffix) {
        return "<PRE> " + prefix + " <SUF>" + suffix + " <MID>";
    }

    @Override
    public String modelFamily() {
        return "codellama";
    }
}
