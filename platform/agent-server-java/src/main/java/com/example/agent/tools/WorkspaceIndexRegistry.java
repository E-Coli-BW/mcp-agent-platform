package com.example.agent.tools;

import com.example.agent.rag.ChunkerRegistry;
import com.example.agent.rag.HybridRetriever;
import com.example.agent.rag.OllamaEmbedder;
import com.example.agent.rag.RagIndexer;
import com.example.agent.rag.SearchResult;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Owns bounded, tenant/workspace-scoped in-memory RAG indexes.
 *
 * <p>Only cache bookkeeping is synchronized globally. Indexing and searching
 * are serialized per cache entry, so work for independent tenants can proceed
 * concurrently without sharing mutable retriever state.</p>
 */
@Component
public class WorkspaceIndexRegistry {

    private static final int DEFAULT_MAX_ENTRIES = 8;
    private static final Duration INDEX_TIMEOUT = Duration.ofSeconds(60);
    private static final int FRESHNESS_RETRIES = 2;

    private final OllamaEmbedder embedder;
    private final ChunkerRegistry chunkerRegistry;
    private final int maxEntries;
    private final Object cacheMutex = new Object();
    private final LinkedHashMap<WorkspaceKey, WorkspaceIndex> entries =
            new LinkedHashMap<>(16, 0.75f, true);

    @Autowired
    public WorkspaceIndexRegistry(OllamaEmbedder embedder, ChunkerRegistry chunkerRegistry) {
        this(embedder, chunkerRegistry, DEFAULT_MAX_ENTRIES);
    }

    WorkspaceIndexRegistry(OllamaEmbedder embedder, ChunkerRegistry chunkerRegistry, int maxEntries) {
        if (maxEntries < 1) {
            throw new IllegalArgumentException("maxEntries must be positive");
        }
        this.embedder = embedder;
        this.chunkerRegistry = chunkerRegistry;
        this.maxEntries = maxEntries;
    }

    public SearchOutcome search(String tenantId, Path workspace, String query, int topK) {
        Path normalizedWorkspace = workspace.toAbsolutePath().normalize();
        String resolvedTenant = tenantId == null || tenantId.isBlank() ? "default" : tenantId;
        WorkspaceIndex index = getOrCreate(new WorkspaceKey(resolvedTenant, normalizedWorkspace));
        return index.search(normalizedWorkspace, query, topK);
    }

    private WorkspaceIndex getOrCreate(WorkspaceKey key) {
        synchronized (cacheMutex) {
            WorkspaceIndex existing = entries.get(key);
            if (existing != null) {
                return existing;
            }
            if (entries.size() >= maxEntries) {
                Map.Entry<WorkspaceKey, WorkspaceIndex> eldest = entries.entrySet().iterator().next();
                entries.remove(eldest.getKey());
            }
            HybridRetriever retriever = new HybridRetriever(embedder);
            RagIndexer indexer = new RagIndexer(chunkerRegistry, embedder, retriever);
            WorkspaceIndex created = new WorkspaceIndex(retriever, indexer);
            entries.put(key, created);
            return created;
        }
    }

    record SearchOutcome(List<SearchResult> results, String unavailableMessage) {
        static SearchOutcome unavailable(String message) {
            return new SearchOutcome(List.of(), message);
        }

        static SearchOutcome found(List<SearchResult> results) {
            return new SearchOutcome(results, null);
        }
    }

    private record WorkspaceKey(String tenantId, Path workspace) {
    }

    private final class WorkspaceIndex {
        private final HybridRetriever retriever;
        private final RagIndexer indexer;
        private final ReentrantLock operationLock = new ReentrantLock();
        private String indexedFingerprint;

        private WorkspaceIndex(HybridRetriever retriever, RagIndexer indexer) {
            this.retriever = retriever;
            this.indexer = indexer;
        }

        private SearchOutcome search(Path workspace, String query, int topK) {
            operationLock.lock();
            try {
                for (int attempt = 0; attempt < FRESHNESS_RETRIES; attempt++) {
                    String before;
                    try {
                        before = fingerprint(workspace);
                    } catch (RuntimeException e) {
                        return SearchOutcome.unavailable(
                                "Cannot inspect RAG workspace " + workspace + ": " + e.getMessage()
                        );
                    }
                    if (!before.equals(indexedFingerprint)) {
                        indexedFingerprint = null;
                        try {
                            indexer.indexWorkspace(workspace).block(INDEX_TIMEOUT);
                            String afterBuild = fingerprint(workspace);
                            if (!before.equals(afterBuild)) {
                                continue;
                            }
                            indexedFingerprint = afterBuild;
                        } catch (RuntimeException e) {
                            return SearchOutcome.unavailable(
                                    "RAG index unavailable for workspace " + workspace + ": " + e.getMessage()
                            );
                        }
                    }

                    if (!retriever.hasIndex()) {
                        return SearchOutcome.unavailable(
                                "No indexable RAG content found in workspace " + workspace
                                        + ". Use file_search instead."
                        );
                    }

                    List<SearchResult> results;
                    try {
                        results = retriever.search(query, topK).block();
                    } catch (RuntimeException e) {
                        return SearchOutcome.unavailable(
                                "RAG search unavailable for workspace " + workspace + ": " + e.getMessage()
                        );
                    }

                    String afterSearch;
                    try {
                        afterSearch = fingerprint(workspace);
                    } catch (RuntimeException e) {
                        return SearchOutcome.unavailable(
                                "Cannot inspect RAG workspace " + workspace + ": " + e.getMessage()
                        );
                    }
                    if (afterSearch.equals(indexedFingerprint)) {
                        return SearchOutcome.found(results == null ? List.of() : results);
                    }
                    indexedFingerprint = null;
                }
                return SearchOutcome.unavailable(
                        "Workspace changed while its RAG index was being refreshed. Retry the search."
                );
            } finally {
                operationLock.unlock();
            }
        }
    }

    /**
     * Metadata-only freshness token over the exact files ChunkerRegistry indexes.
     * It still walks the workspace tree on each query, but does not read file
     * contents. Path, size, and modification time detect ordinary editor/build
     * changes; an explicit workspace revision or file-change stream would give
     * stronger freshness with less per-query scanning in a production indexer.
     */
    private String fingerprint(Path workspace) {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 is unavailable", impossible);
        }

        List<String> records = new ArrayList<>();
        try {
            Files.walkFileTree(workspace, new SimpleFileVisitor<>() {
                @Override
                public FileVisitResult preVisitDirectory(Path directory, BasicFileAttributes attrs) {
                    if (chunkerRegistry.shouldSkipDirectory(directory)) {
                        return FileVisitResult.SKIP_SUBTREE;
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                    if (!chunkerRegistry.isIndexableFile(file)) {
                        return FileVisitResult.CONTINUE;
                    }
                    Path relative = workspace.relativize(file);
                    records.add(relative + "\u0000" + attrs.size() + "\u0000"
                            + attrs.lastModifiedTime());
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException e) {
            throw new IllegalStateException("Cannot inspect workspace " + workspace, e);
        }
        records.sort(String::compareTo);
        for (String record : records) {
            digest.update(record.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            digest.update((byte) '\n');
        }
        return HexFormat.of().formatHex(digest.digest());
    }
}
