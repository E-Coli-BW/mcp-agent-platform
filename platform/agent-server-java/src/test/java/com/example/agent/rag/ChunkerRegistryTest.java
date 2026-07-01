package com.example.agent.rag;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

class ChunkerRegistryTest {

    @TempDir
    Path tempDir;

    @Test
    void should_dispatchToMarkdown_when_mdExtension() throws IOException {
        TreeSitterChunker treeSitter = new TreeSitterChunker();
        FixedSizeChunker fixedSize = new FixedSizeChunker();
        MarkdownChunker markdown = new MarkdownChunker();
        OpenApiChunker openApi = new OpenApiChunker(fixedSize);
        HtmlChunker html = new HtmlChunker(fixedSize);
        ChunkerRegistry registry = new ChunkerRegistry(
                treeSitter, markdown, openApi, fixedSize, html
        );

        Path mdFile = tempDir.resolve("readme.md");
        Files.writeString(mdFile, "# Title\nContent\n## Section\nMore content");

        List<CodeChunk> chunks = registry.chunkFile(mdFile);

        assertEquals(2, chunks.size());
        assertEquals("markdown", chunks.get(0).language());
    }

    @Test
    void should_dispatchToFixedSize_when_txtExtension() throws IOException {
        TreeSitterChunker treeSitter = new TreeSitterChunker();
        FixedSizeChunker fixedSize = new FixedSizeChunker();
        MarkdownChunker markdown = new MarkdownChunker();
        OpenApiChunker openApi = new OpenApiChunker(fixedSize);
        HtmlChunker html = new HtmlChunker(fixedSize);
        ChunkerRegistry registry = new ChunkerRegistry(
                treeSitter, markdown, openApi, fixedSize, html
        );

        Path txtFile = tempDir.resolve("notes.txt");
        Files.writeString(txtFile, "Some plain text content");

        List<CodeChunk> chunks = registry.chunkFile(txtFile);

        assertFalse(chunks.isEmpty());
        assertEquals("text_block", chunks.get(0).chunkType());
    }
}
