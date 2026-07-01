package com.example.memoryserver.search;

import com.example.memoryserver.model.MemoryEntity;
import org.springframework.stereotype.Component;

import java.util.*;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * TF-IDF style keyword search with prefix matching and tag bonus.
 * Same algorithm as the TypeScript version for consistent cross-language results.
 */
@Component
public class MemorySearchEngine {

    private static final Pattern WORD_SPLIT = Pattern.compile("[^\\p{L}\\p{N}]+");

    /** Scored search result. */
    public record ScoredResult(MemoryEntity entity, double score) {}

    /** Tokenize text into lowercase words, filtering out single chars. */
    public List<String> tokenize(String text) {
        return Arrays.stream(WORD_SPLIT.split(text.toLowerCase()))
                .filter(t -> t.length() > 1)
                .collect(Collectors.toList());
    }

    /**
     * Search a list of entities by query keywords and optional tag filter.
     * Returns top results sorted by relevance score.
     */
    public List<ScoredResult> search(List<MemoryEntity> entries, String query,
                                     List<String> tags, int limit) {
        List<String> queryTokens = tokenize(query);
        if (queryTokens.isEmpty()) return List.of();

        return entries.stream()
                .map(e -> new ScoredResult(e, score(e, queryTokens, tags)))
                .filter(s -> s.score > 0)
                .sorted((a, b) -> Double.compare(b.score, a.score))
                .limit(limit)
                .toList();
    }

    /** Calculate relevance score for a single entry. */
    double score(MemoryEntity entry, List<String> queryTokens, List<String> queryTags) {
        List<String> entryTokens = tokenize(entry.getContent() + " " + entry.getKey());
        Map<String, Long> tf = entryTokens.stream()
                .collect(Collectors.groupingBy(t -> t, Collectors.counting()));

        // Term overlap + prefix matching bonus
        double termScore = 0;
        for (String qt : queryTokens) {
            termScore += tf.getOrDefault(qt, 0L);
            for (var e : tf.entrySet()) {
                if (e.getKey().startsWith(qt) || qt.startsWith(e.getKey())) {
                    termScore += e.getValue() * 0.5;
                }
            }
        }
        termScore = entryTokens.isEmpty() ? 0 : termScore / Math.sqrt(entryTokens.size());

        // Tag match bonus
        double tagScore = 0;
        if (queryTags != null && !queryTags.isEmpty()) {
            Set<String> entryTagSet = entry.getTags().stream()
                    .map(String::toLowerCase).collect(Collectors.toSet());
            for (String qt : queryTags) {
                if (entryTagSet.contains(qt.toLowerCase())) tagScore += 2;
            }
        }

        return termScore + tagScore;
    }
}
