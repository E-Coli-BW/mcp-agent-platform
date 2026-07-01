package com.example.completion.fim;

import com.example.completion.config.CompletionProperties;
import org.springframework.stereotype.Component;

/**
 * Extracts prefix and suffix from file content based on cursor position.
 * Applies line budget to keep FIM prompt within context window limits.
 */
@Component
public class FimContextBuilder {

    private final CompletionProperties props;

    public FimContextBuilder(CompletionProperties props) {
        this.props = props;
    }

    public record FimContext(String prefix, String suffix) {}

    /**
     * Build FIM context from file content and cursor position.
     *
     * @param fileContent full file text
     * @param cursorLine 0-based line number
     * @param cursorColumn 0-based column number
     * @return prefix (before cursor) and suffix (after cursor)
     */
    public FimContext build(String fileContent, int cursorLine, int cursorColumn) {
        String[] lines = fileContent.split("\n", -1);

        // Clamp cursor
        cursorLine = Math.max(0, Math.min(cursorLine, lines.length - 1));
        String currentLine = lines[cursorLine];
        cursorColumn = Math.max(0, Math.min(cursorColumn, currentLine.length()));

        // Build prefix: last N lines before cursor + partial current line
        int prefixStart = Math.max(0, cursorLine - props.getFim().getMaxPrefixLines());
        StringBuilder prefix = new StringBuilder();
        for (int i = prefixStart; i < cursorLine; i++) {
            prefix.append(lines[i]).append('\n');
        }
        prefix.append(currentLine, 0, cursorColumn);

        // Build suffix: rest of current line + next N lines
        StringBuilder suffix = new StringBuilder();
        suffix.append(currentLine.substring(cursorColumn));
        int suffixEnd = Math.min(lines.length, cursorLine + 1 + props.getFim().getMaxSuffixLines());
        for (int i = cursorLine + 1; i < suffixEnd; i++) {
            suffix.append('\n').append(lines[i]);
        }

        return new FimContext(prefix.toString(), suffix.toString());
    }

    /**
     * Build FIM context from pre-split prefix/suffix (e.g., from OpenAI-style prompt).
     */
    public FimContext buildFromPrefixSuffix(String prefix, String suffix) {
        // Truncate to line budget
        String[] prefixLines = prefix.split("\n", -1);
        if (prefixLines.length > props.getFim().getMaxPrefixLines()) {
            prefix = String.join("\n",
                    java.util.Arrays.copyOfRange(prefixLines,
                            prefixLines.length - props.getFim().getMaxPrefixLines(),
                            prefixLines.length));
        }

        String[] suffixLines = suffix.split("\n", -1);
        if (suffixLines.length > props.getFim().getMaxSuffixLines()) {
            suffix = String.join("\n",
                    java.util.Arrays.copyOfRange(suffixLines, 0,
                            props.getFim().getMaxSuffixLines()));
        }

        return new FimContext(prefix, suffix);
    }
}
