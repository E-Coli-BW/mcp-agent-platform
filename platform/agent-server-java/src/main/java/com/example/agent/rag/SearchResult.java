package com.example.agent.rag;

/**
 * Ranked retrieval result.
 */
public record SearchResult(CodeChunk chunk, double score) implements Comparable<SearchResult> {
    @Override
    public int compareTo(SearchResult other) {
        return Double.compare(other.score(), this.score());
    }
}
