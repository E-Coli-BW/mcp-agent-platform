package com.example.completion.fim;

/**
 * FIM (Fill-in-the-Middle) format builder.
 * 
 * Different models use different special tokens for FIM prompting.
 * This interface abstracts the format so we can swap models easily.
 */
public interface FimFormatter {

    /**
     * Build a FIM prompt from prefix (text before cursor) and suffix (text after cursor).
     *
     * @param prefix code before the cursor
     * @param suffix code after the cursor
     * @return formatted FIM prompt string
     */
    String format(String prefix, String suffix);

    /**
     * Model identifier this formatter targets.
     */
    String modelFamily();
}
