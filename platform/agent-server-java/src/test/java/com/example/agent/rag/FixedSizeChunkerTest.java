package com.example.agent.rag;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class FixedSizeChunkerTest {

    private final FixedSizeChunker chunker = new FixedSizeChunker();

    @TempDir
    Path tempDir;

    @Test
    void should_splitIntoChunks_when_fileExceedsMaxLines() throws IOException {
        String content = IntStream.rangeClosed(1, 100)
                .mapToObj(i -> "Line " + i)
                .collect(Collectors.joining("\n"));

        Path file = tempDir.resolve("large.txt");
        Files.writeString(file, content);

        List<CodeChunk> chunks = chunker.chunk(file, 50, 5);

        assertTrue(chunks.size() >= 2);
        assertEquals("text_block", chunks.get(0).chunkType());
        assertEquals(1, chunks.get(0).startLine());
        assertEquals(50, chunks.get(0).endLine());
        assertEquals(46, chunks.get(1).startLine());
    }

    @Test
    void should_returnSingleChunk_when_fileSmallEnough() throws IOException {
        String content = "Line 1\nLine 2\nLine 3";

        Path file = tempDir.resolve("small.txt");
        Files.writeString(file, content);

        List<CodeChunk> chunks = chunker.chunk(file, 50, 5);

        assertEquals(1, chunks.size());
    }
}
