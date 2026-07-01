package com.example.mcp.common.util;

/**
 * Token-aware result truncation.
 * Shared across all MCP services to prevent oversized tool responses.
 */
public final class ResultTruncator {
    private static final int CHARS_PER_TOKEN = 3;
    private static final int DEFAULT_MAX_TOKENS = 4000;

    private ResultTruncator() {}

    public static String truncate(String text, int maxTokens) {
        if (text == null) return "";
        int maxChars = maxTokens * CHARS_PER_TOKEN;
        if (text.length() <= maxChars) return text;
        int remaining = text.length() - maxChars;
        return text.substring(0, maxChars) + "\n\n[... truncated, " + remaining + " more chars]";
    }

    public static String truncate(String text) {
        return truncate(text, DEFAULT_MAX_TOKENS);
    }

    public static String truncateLines(String text, int maxLines) {
        if (text == null) return "";
        String[] lines = text.split("\n", -1);
        if (lines.length <= maxLines) return text;
        var sb = new StringBuilder();
        for (int i = 0; i < maxLines; i++) sb.append(lines[i]).append('\n');
        sb.append("\n[... ").append(lines.length - maxLines).append(" more lines]");
        return sb.toString();
    }
}
