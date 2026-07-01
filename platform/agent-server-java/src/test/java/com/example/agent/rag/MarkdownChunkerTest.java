package com.example.agent.rag;

import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class MarkdownChunkerTest {

    private final MarkdownChunker chunker = new MarkdownChunker();

    @Test
    void should_splitByHeadings_when_multipleHeadingsPresent() {
        String content = """
                # Introduction
                Some intro text.
                
                ## Getting Started
                Setup instructions here.
                
                ## API Reference
                API docs here.
                """;

        List<CodeChunk> chunks = chunker.chunkContent(content, "test.md", Instant.now());

        assertEquals(3, chunks.size());
        assertEquals("Introduction", chunks.get(0).name());
        assertEquals("Getting Started", chunks.get(1).name());
        assertEquals("API Reference", chunks.get(2).name());
        assertEquals("section", chunks.get(0).chunkType());
        assertEquals("markdown", chunks.get(0).language());
    }

    @Test
    void should_returnWholeFile_when_noHeadingsPresent() {
        String content = "Just some plain text\nwith multiple lines\nbut no headings.";

        List<CodeChunk> chunks = chunker.chunkContent(content, "notes.md", Instant.now());

        assertEquals(1, chunks.size());
        assertEquals("document", chunks.get(0).chunkType());
        assertEquals("notes.md", chunks.get(0).name());
    }
}
