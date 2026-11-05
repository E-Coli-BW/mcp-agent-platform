package com.example.agent.tools;

import com.example.agent.rag.ChunkerRegistry;
import com.example.agent.rag.CodeChunk;
import com.example.agent.rag.FixedSizeChunker;
import com.example.agent.rag.HtmlChunker;
import com.example.agent.rag.MarkdownChunker;
import com.example.agent.rag.OpenApiChunker;
import com.example.agent.rag.OllamaEmbedder;
import com.example.agent.rag.TreeSitterChunker;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.spy;

class WorkspaceIndexRegistryTest {

    @TempDir
    Path workspace;

    @Test
    void should_keepSeparateIndexesForDifferentTenantsEvenWhenTheyShareAPath() throws Exception {
        Path source = Files.writeString(workspace.resolve("source.java"), "source");
        ChunkerRegistry chunker = stubChunker();
        OllamaEmbedder embedder = stubEmbedder();
        CodeChunk tenantAChunk = chunk(source, "tenant-a marker");
        CodeChunk tenantBChunk = chunk(source, "tenant-b marker");
        doReturn(List.of(tenantAChunk), List.of(tenantBChunk))
                .when(chunker).chunkDirectory(workspace);
        WorkspaceIndexRegistry registry = new WorkspaceIndexRegistry(embedder, chunker);

        var tenantA = registry.search("tenant-a", workspace, "tenant-a", 5);
        var tenantB = registry.search("tenant-b", workspace, "tenant-b", 5);

        assertEquals("tenant-a marker", tenantA.results().getFirst().chunk().content());
        assertEquals("tenant-b marker", tenantB.results().getFirst().chunk().content());
        verify(chunker, times(2)).chunkDirectory(workspace);
    }

    @Test
    void should_rebuildIndexWhenWorkspaceMetadataChanges() throws Exception {
        Path source = Files.writeString(workspace.resolve("source.java"), "v1");
        ChunkerRegistry chunker = stubChunker();
        OllamaEmbedder embedder = stubEmbedder();
        doReturn(List.of(chunk(source, "old source")), List.of(chunk(source, "updated source")))
                .when(chunker).chunkDirectory(workspace);
        WorkspaceIndexRegistry registry = new WorkspaceIndexRegistry(embedder, chunker);

        var oldIndex = registry.search("tenant-a", workspace, "source", 5);
        Files.writeString(source, "v2 has changed size");
        var newIndex = registry.search("tenant-a", workspace, "source", 5);

        assertEquals("old source", oldIndex.results().getFirst().chunk().content());
        assertEquals("updated source", newIndex.results().getFirst().chunk().content());
        verify(chunker, times(2)).chunkDirectory(workspace);
    }

    @Test
    void should_ignoreChangesToFilesTheIndexerDoesNotChunk() throws Exception {
        Path source = Files.writeString(workspace.resolve("source.java"), "source");
        Path log = Files.writeString(workspace.resolve("notes.log"), "first note");
        ChunkerRegistry chunker = stubChunker();
        OllamaEmbedder embedder = stubEmbedder();
        doReturn(List.of(chunk(source, "source"))).when(chunker).chunkDirectory(workspace);
        WorkspaceIndexRegistry registry = new WorkspaceIndexRegistry(embedder, chunker);

        registry.search("tenant-a", workspace, "source", 5);
        Files.writeString(log, "a changed but unindexed note");
        registry.search("tenant-a", workspace, "source", 5);

        verify(chunker).chunkDirectory(workspace);
    }

    @Test
    void should_evictLeastRecentlyUsedIndexAtConfiguredCapacity() throws Exception {
        Path workspaceA = Files.createDirectory(workspace.resolve("a"));
        Path workspaceB = Files.createDirectory(workspace.resolve("b"));
        Files.writeString(workspaceA.resolve("source.java"), "a");
        Files.writeString(workspaceB.resolve("source.java"), "b");
        ChunkerRegistry chunker = stubChunker();
        OllamaEmbedder embedder = stubEmbedder();
        doAnswer(invocation -> {
            Path current = invocation.getArgument(0);
            return List.of(chunk(current.resolve("source.java"), current.getFileName().toString()));
        }).when(chunker).chunkDirectory(any(Path.class));
        WorkspaceIndexRegistry registry = new WorkspaceIndexRegistry(embedder, chunker, 1);

        registry.search("tenant-a", workspaceA, "source", 5);
        registry.search("tenant-b", workspaceB, "source", 5);
        registry.search("tenant-a", workspaceA, "source", 5);

        verify(chunker, times(2)).chunkDirectory(workspaceA);
        verify(chunker).chunkDirectory(workspaceB);
    }

    @Test
    void should_reportIndexFailureInsteadOfSearchingPreviousSnapshot() throws Exception {
        Files.writeString(workspace.resolve("source.java"), "source");
        ChunkerRegistry chunker = mock(ChunkerRegistry.class);
        OllamaEmbedder embedder = stubEmbedder();
        doThrow(new IllegalStateException("fixture failure"))
                .when(chunker).chunkDirectory(workspace);
        WorkspaceIndexRegistry registry = new WorkspaceIndexRegistry(embedder, chunker);

        var result = registry.search("tenant-a", workspace, "source", 5);

        assertFalse(result.unavailableMessage().isBlank());
        assertEquals(List.of(), result.results());
    }

    private static OllamaEmbedder stubEmbedder() {
        OllamaEmbedder embedder = mock(OllamaEmbedder.class);
        float[] vector = {1.0f, 0.0f};
        when(embedder.embedBatch(anyList(), eq(10))).thenAnswer(invocation -> {
            List<?> texts = invocation.getArgument(0);
            return Flux.fromIterable(texts).map(ignored -> vector);
        });
        when(embedder.embed(anyString())).thenReturn(Mono.just(vector));
        return embedder;
    }

    private static ChunkerRegistry stubChunker() {
        return spy(new ChunkerRegistry(
                mock(TreeSitterChunker.class),
                mock(MarkdownChunker.class),
                mock(OpenApiChunker.class),
                mock(FixedSizeChunker.class),
                mock(HtmlChunker.class)));
    }

    private static CodeChunk chunk(Path path, String content) {
        return new CodeChunk(content, path.toString(), "java", "class", "Fixture", 1, 1);
    }
}
