package com.example.agent.rag;

import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Chunker for Markdown files — splits by heading boundaries.
 */
@Component
public class MarkdownChunker {

    private static final Pattern HEADING_PATTERN = Pattern.compile(
            "^(#{1,3}\\s+.+)$", Pattern.MULTILINE
    );

    /**
     * Chunk a markdown file by headings.
     */
    public List<CodeChunk> chunk(Path filePath) {
        try {
            String content = Files.readString(filePath);
            Instant modified = Files.getLastModifiedTime(filePath).toInstant();
            return chunkContent(content, filePath.toString(), modified);
        } catch (IOException e) {
            return List.of();
        }
    }

    List<CodeChunk> chunkContent(String content, String filePath, Instant modified) {
        Matcher matcher = HEADING_PATTERN.matcher(content);
        List<int[]> headingPositions = new ArrayList<>();

        while (matcher.find()) {
            headingPositions.add(new int[]{matcher.start(), matcher.end()});
        }

        if (headingPositions.isEmpty()) {
            int totalLines = content.split("\\n", -1).length;
            return List.of(new CodeChunk(
                    content, filePath, "markdown", "document",
                    Path.of(filePath).getFileName().toString(),
                    1, totalLines, modified, null
            ));
        }

        List<CodeChunk> chunks = new ArrayList<>();
        for (int i = 0; i < headingPositions.size(); i++) {
            int start = headingPositions.get(i)[0];
            int end = (i + 1 < headingPositions.size())
                    ? headingPositions.get(i + 1)[0]
                    : content.length();

            String section = content.substring(start, end).stripTrailing();
            String heading = content.substring(
                    headingPositions.get(i)[0], headingPositions.get(i)[1]
            );
            String name = heading.replaceFirst("^#+\\s+", "").trim();

            int startLine = countNewlines(content, 0, start) + 1;
            int endLine = countEndLine(content, end);

            chunks.add(new CodeChunk(
                    section, filePath, "markdown", "section",
                    name, startLine, endLine, modified, null
            ));
        }
        return chunks;
    }

    private int countNewlines(String text, int from, int to) {
        int count = 0;
        for (int i = from; i < to && i < text.length(); i++) {
            if (text.charAt(i) == '\n') {
                count++;
            }
        }
        return count;
    }

    private int countEndLine(String text, int end) {
        int line = countNewlines(text, 0, end);
        if (end == text.length() && !text.isEmpty() && text.charAt(text.length() - 1) != '\n') {
            return line + 1;
        }
        return Math.max(1, line);
    }
}
