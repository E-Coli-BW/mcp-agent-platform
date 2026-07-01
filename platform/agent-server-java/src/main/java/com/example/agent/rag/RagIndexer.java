package com.example.agent.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;

import java.nio.file.Path;
import java.util.List;

/**
 * Indexes a workspace for in-memory RAG search.
 */
@Component
public class RagIndexer {

    private static final Logger log = LoggerFactory.getLogger(RagIndexer.class);

    private final ChunkerRegistry chunkerRegistry;
    private final OllamaEmbedder embedder;
    private final HybridRetriever retriever;

    public RagIndexer(ChunkerRegistry chunkerRegistry, OllamaEmbedder embedder, HybridRetriever retriever) {
        this.chunkerRegistry = chunkerRegistry;
        this.embedder = embedder;
        this.retriever = retriever;
    }

    /**
     * Builds an index for the supplied workspace.
     */
    public Mono<Void> indexWorkspace(Path directory) {
        log.info("Indexing workspace: {}", directory);
        List<CodeChunk> chunks = chunkerRegistry.chunkDirectory(directory);
        log.info("Chunked {} chunks from {}", chunks.size(), directory);
        if (chunks.isEmpty()) {
            return Mono.empty();
        }
        List<String> texts = chunks.stream().map(CodeChunk::content).toList();
        return embedder.embedBatch(texts, 10).collectList().doOnNext(embeddings -> retriever.buildIndex(chunks, embeddings)).then();
    }
}
