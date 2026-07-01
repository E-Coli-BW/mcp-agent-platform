package com.example.agent.rag;

import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

/**
 * Fixed-size line-based chunker with configurable overlap.
 */
@Component
public class FixedSizeChunker {

    private static final int DEFAULT_MAX_LINES = 50;
    private static final int DEFAULT_OVERLAP = 5;

    /**
     * Chunk a file into fixed-size line-based blocks.
     */
    public List<CodeChunk> chunk(Path filePath) {
        return chunk(filePath, DEFAULT_MAX_LINES, DEFAULT_OVERLAP);
    }

    /**
     * Chunk a file with configurable max lines and overlap.
     */
    public List<CodeChunk> chunk(Path filePath, int maxLines, int overlap) {
        try {
            List<String> lines = Files.readAllLines(filePath);
            Instant modified = Files.getLastModifiedTime(filePath).toInstant();
            String filePathStr = filePath.toString();
            String filename = filePath.getFileName().toString();
            String ext = filename.contains(".") ? filename.substring(filename.lastIndexOf('.') + 1) : "text";

            if (lines.size() <= maxLines) {
                return List.of(new CodeChunk(
                        String.join("\n", lines), filePathStr, ext, "text_block",
                        filename + ":1-" + lines.size(), 1, lines.size(), modified, null
                ));
            }

            List<CodeChunk> chunks = new ArrayList<>();
            int start = 0;
            int step = Math.max(1, maxLines - overlap);
            while (start < lines.size()) {
                int end = Math.min(start + maxLines, lines.size());
                String content = String.join("\n", lines.subList(start, end));
                String name = filename + ":" + (start + 1) + "-" + end;
                chunks.add(new CodeChunk(
                        content, filePathStr, ext, "text_block",
                        name, start + 1, end, modified, null
                ));
                if (end == lines.size()) {
                    break;
                }
                start += step;
            }
            return chunks;
        } catch (IOException e) {
            return List.of();
        }
    }
}
