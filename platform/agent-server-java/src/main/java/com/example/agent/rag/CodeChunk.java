package com.example.agent.rag;

import java.time.Instant;

/**
 * A chunk of code extracted from a source file via tree-sitter AST parsing.
 */
public record CodeChunk(
        String content,
        String filePath,
        String language,
        String chunkType,
        String name,
        int startLine,
        int endLine,
        Instant lastModified,
        String docstring
) {
    public CodeChunk(String content, String filePath, String language,
                     String chunkType, String name, int startLine, int endLine) {
        this(content, filePath, language, chunkType, name, startLine, endLine, Instant.now(), null);
    }
}
