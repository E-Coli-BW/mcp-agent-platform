package com.example.agent.rag;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertTrue;

class OpenApiChunkerTest {

    private final FixedSizeChunker fixedSizeChunker = new FixedSizeChunker();
    private final OpenApiChunker chunker = new OpenApiChunker(fixedSizeChunker);

    @TempDir
    Path tempDir;

    @Test
    void should_extractEndpoints_when_validOpenApiSpec() throws IOException {
        String spec = """
                openapi: "3.0.0"
                info:
                  title: Test API
                  version: "1.0"
                paths:
                  /users:
                    get:
                      summary: List users
                      description: Returns all users
                  /users/{id}:
                    delete:
                      summary: Delete user
                """;

        Path file = tempDir.resolve("api.yaml");
        Files.writeString(file, spec);

        List<CodeChunk> chunks = chunker.chunk(file);

        assertTrue(chunks.stream().anyMatch(c -> c.name().equals("GET /users")));
        assertTrue(chunks.stream().anyMatch(c -> c.name().equals("DELETE /users/{id}")));
        assertTrue(chunks.stream().allMatch(c ->
                c.chunkType().equals("api_endpoint") || c.chunkType().equals("api_schema")
        ));
    }

    @Test
    void should_fallBackToFixedSize_when_notOpenApiSpec() throws IOException {
        String content = """
                database:
                  host: localhost
                  port: 5432
                """;

        Path file = tempDir.resolve("config.yaml");
        Files.writeString(file, content);

        List<CodeChunk> chunks = chunker.chunk(file);

        assertTrue(chunks.stream().allMatch(c -> c.chunkType().equals("text_block")));
    }
}
