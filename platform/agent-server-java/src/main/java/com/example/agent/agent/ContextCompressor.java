package com.example.agent.agent;

import com.example.agent.rag.AstCompressor;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;

/**
 * Context-aware compression for tool output.
 */
@Component
public class ContextCompressor {

    private final AstCompressor astCompressor;

    public ContextCompressor() {
        this(new AstCompressor());
    }

    public ContextCompressor(AstCompressor astCompressor) {
        this.astCompressor = astCompressor;
    }

    /**
     * Compresses tool output based on its shape and source tool.
     */
    public String smartCompress(String content, int maxChars, String toolName) {
        if (maxChars <= 0) {
            return "";
        }
        if (content == null) {
            return null;
        }
        if ("file_read".equals(toolName) || "rag_search".equals(toolName)) {
            return astCompressor.compress(content, maxChars, toolName);
        }
        if ("file_list".equals(toolName) || "file_search".equals(toolName)) {
            List<String> lines = content.lines().toList();
            if (lines.size() <= 20) {
                return content;
            }
            List<String> compressed = new ArrayList<>();
            compressed.addAll(lines.subList(0, 10));
            compressed.add("... (" + (lines.size() - 20) + " lines omitted) ...");
            compressed.addAll(lines.subList(lines.size() - 10, lines.size()));
            return String.join("\n", compressed);
        }
        if (content.length() <= maxChars) {
            return content;
        }
        int half = maxChars / 2;
        int omitted = content.length() - maxChars;
        return content.substring(0, half)
                + "\n...(truncated " + omitted + " chars)...\n"
                + content.substring(content.length() - half);
    }
}
