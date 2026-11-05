package com.example.agent.rag;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class RagIndexerTest {

    @TempDir
    Path workspace;

    @Test
    void should_clearPreviousIndexWhenCurrentWorkspaceHasNoContent() {
        ChunkerRegistry chunkerRegistry = mock(ChunkerRegistry.class);
        OllamaEmbedder embedder = mock(OllamaEmbedder.class);
        HybridRetriever retriever = mock(HybridRetriever.class);
        when(chunkerRegistry.chunkDirectory(workspace)).thenReturn(List.of());

        new RagIndexer(chunkerRegistry, embedder, retriever)
                .indexWorkspace(workspace)
                .block();

        verify(retriever).buildIndex(List.of(), List.of());
        verifyNoInteractions(embedder);
    }
}