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
 * HTML chunker — splits by heading tags, strips HTML tags from content.
 */
@Component
public class HtmlChunker {

    private static final Pattern HEADING_PATTERN = Pattern.compile(
            "<h([1-3])[^>]*>(.*?)</h\\1>", Pattern.CASE_INSENSITIVE | Pattern.DOTALL
    );
    private static final Pattern TAG_PATTERN = Pattern.compile("<[^>]+>");

    private final FixedSizeChunker fixedSizeChunker;

    public HtmlChunker(FixedSizeChunker fixedSizeChunker) {
        this.fixedSizeChunker = fixedSizeChunker;
    }

    /**
     * Chunk an HTML file by heading tags.
     */
    public List<CodeChunk> chunk(Path filePath) {
        try {
            String content = Files.readString(filePath);
            Instant modified = Files.getLastModifiedTime(filePath).toInstant();
            return chunkContent(content, filePath.toString(), modified, filePath);
        } catch (IOException e) {
            return List.of();
        }
    }

    List<CodeChunk> chunkContent(String content, String filePath, Instant modified, Path path) {
        Matcher matcher = HEADING_PATTERN.matcher(content);
        List<int[]> headingPositions = new ArrayList<>();
        List<String> headingNames = new ArrayList<>();

        while (matcher.find()) {
            headingPositions.add(new int[]{matcher.start(), matcher.end()});
            headingNames.add(stripTags(matcher.group(2)).trim());
        }

        if (headingPositions.isEmpty()) {
            return fixedSizeChunker.chunk(path);
        }

        List<CodeChunk> chunks = new ArrayList<>();
        for (int i = 0; i < headingPositions.size(); i++) {
            int start = headingPositions.get(i)[0];
            int end = (i + 1 < headingPositions.size())
                    ? headingPositions.get(i + 1)[0]
                    : content.length();

            String section = content.substring(start, end);
            String stripped = stripTags(section).trim();
            String name = headingNames.get(i);

            int startLine = countNewlines(content, 0, start) + 1;
            int endLine = countEndLine(content, end);

            chunks.add(new CodeChunk(
                    stripped, filePath, "html", "section",
                    name, startLine, endLine, modified, null
            ));
        }
        return chunks;
    }

    private String stripTags(String text) {
        return TAG_PATTERN.matcher(text).replaceAll("");
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
