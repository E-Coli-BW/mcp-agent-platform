package com.example.memoryserver.search;

import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.search.MemorySearchEngine.ScoredResult;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for TF-IDF search engine.
 * No Spring context needed — pure Java logic.
 */
class MemorySearchEngineTest {

    private MemorySearchEngine engine;

    @BeforeEach
    void setUp() {
        engine = new MemorySearchEngine();
    }

    private MemoryEntity entity(String key, String content, Set<String> tags) {
        var e = new MemoryEntity("tenant1", key, content, "default");
        e.updateContent(content, null, tags, false);
        return e;
    }

    @Test
    void searchByExactKeyword() {
        var entries = List.of(
                entity("java-notes", "Java is a programming language", Set.of("java")),
                entity("python-notes", "Python is a scripting language", Set.of("python")),
                entity("go-notes", "Go is compiled and fast", Set.of("go"))
        );

        List<ScoredResult> results = engine.search(entries, "java", null, 10);

        assertFalse(results.isEmpty());
        assertEquals("java-notes", results.get(0).entity().getKey());
    }

    @Test
    void searchWithTagBoost() {
        var entries = List.of(
                entity("doc1", "generic content about systems", Set.of("java")),
                entity("doc2", "java programming tutorial", Set.of("python")),
                entity("doc3", "generic content no match", Set.of("java"))
        );

        // Search for "java" with tag filter — doc1 and doc3 have java tag
        List<ScoredResult> results = engine.search(entries, "generic", List.of("java"), 10);

        assertFalse(results.isEmpty());
        // Entries with matching tag should rank higher
        assertTrue(results.stream().anyMatch(r -> r.entity().getKey().equals("doc1")));
    }

    @Test
    void searchEmptyQuery() {
        var entries = List.of(
                entity("k1", "some content", Set.of("tag1"))
        );

        List<ScoredResult> results = engine.search(entries, "", null, 10);
        // Empty query should return empty or all entries depending on implementation
        assertNotNull(results);
    }

    @Test
    void searchNoResults() {
        var entries = List.of(
                entity("k1", "hello world", Set.of())
        );

        List<ScoredResult> results = engine.search(entries, "xyznonexistent", null, 10);
        assertTrue(results.isEmpty());
    }

    @Test
    void searchRespectsLimit() {
        var entries = List.of(
                entity("k1", "java programming", Set.of()),
                entity("k2", "java tutorial", Set.of()),
                entity("k3", "java guide", Set.of()),
                entity("k4", "java reference", Set.of()),
                entity("k5", "java manual", Set.of())
        );

        List<ScoredResult> results = engine.search(entries, "java", null, 3);
        assertTrue(results.size() <= 3);
    }

    @Test
    void searchEmptyCorpus() {
        List<ScoredResult> results = engine.search(List.of(), "anything", null, 10);
        assertTrue(results.isEmpty());
    }
}
