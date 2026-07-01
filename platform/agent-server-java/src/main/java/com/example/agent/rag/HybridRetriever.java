package com.example.agent.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Hybrid retriever combining vector similarity and BM25.
 */
@Component
public class HybridRetriever {

    private static final Logger log = LoggerFactory.getLogger(HybridRetriever.class);
    private static final int RRF_K = 60;

    private final OllamaEmbedder embedder;
    private List<CodeChunk> chunks = new ArrayList<>();
    private List<float[]> embeddings = new ArrayList<>();
    private Map<String, Map<Integer, Integer>> invertedIndex = new HashMap<>();
    private int[] docLengths = new int[0];
    private double avgDocLength = 0;

    public HybridRetriever(OllamaEmbedder embedder) {
        this.embedder = embedder;
    }

    /**
     * Builds the in-memory retrieval index.
     */
    public void buildIndex(List<CodeChunk> chunks, List<float[]> embeddings) {
        this.chunks = new ArrayList<>(chunks);
        this.embeddings = new ArrayList<>(embeddings);
        buildBm25Index();
        log.info("Built hybrid index: {} chunks", chunks.size());
    }

    /**
     * Returns whether an index is currently loaded.
     */
    public boolean hasIndex() {
        return !chunks.isEmpty();
    }

    /**
     * Searches the loaded index.
     */
    public Mono<List<SearchResult>> search(String query, int topK) {
        if (chunks.isEmpty()) {
            return Mono.just(List.of());
        }
        return embedder.embed(query).map(queryEmbedding -> rrfMerge(vectorSearch(queryEmbedding, topK), bm25Search(query, topK), topK));
    }

    private List<SearchResult> vectorSearch(float[] queryEmbedding, int topK) {
        List<SearchResult> results = new ArrayList<>();
        int size = Math.min(chunks.size(), embeddings.size());
        for (int i = 0; i < size; i++) {
            results.add(new SearchResult(chunks.get(i), cosineSimilarity(queryEmbedding, embeddings.get(i))));
        }
        results.sort(Comparator.comparingDouble(SearchResult::score).reversed());
        return results.subList(0, Math.min(topK, results.size()));
    }

    private List<SearchResult> bm25Search(String query, int topK) {
        String[] terms = query.toLowerCase().split("\\s+");
        Map<Integer, Double> scores = new HashMap<>();
        int totalDocuments = chunks.size();
        for (String term : terms) {
            Map<Integer, Integer> postings = invertedIndex.getOrDefault(term, Map.of());
            double idf = Math.log((totalDocuments - postings.size() + 0.5) / (postings.size() + 0.5) + 1.0);
            for (Map.Entry<Integer, Integer> entry : postings.entrySet()) {
                int docId = entry.getKey();
                int tf = entry.getValue();
                double k1 = 1.5;
                double b = 0.75;
                double tfNorm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * docLengths[docId] / avgDocLength));
                scores.merge(docId, idf * tfNorm, Double::sum);
            }
        }
        return scores.entrySet().stream().sorted(Map.Entry.<Integer, Double>comparingByValue().reversed()).limit(topK).map(entry -> new SearchResult(chunks.get(entry.getKey()), entry.getValue())).toList();
    }

    private List<SearchResult> rrfMerge(List<SearchResult> vectorResults, List<SearchResult> bm25Results, int topK) {
        Map<String, Double> scores = new HashMap<>();
        Map<String, CodeChunk> chunkMap = new HashMap<>();
        for (int i = 0; i < vectorResults.size(); i++) {
            SearchResult result = vectorResults.get(i);
            String key = result.chunk().filePath() + ":" + result.chunk().startLine();
            scores.merge(key, 1.0 / (RRF_K + i + 1), Double::sum);
            chunkMap.put(key, result.chunk());
        }
        for (int i = 0; i < bm25Results.size(); i++) {
            SearchResult result = bm25Results.get(i);
            String key = result.chunk().filePath() + ":" + result.chunk().startLine();
            scores.merge(key, 1.0 / (RRF_K + i + 1), Double::sum);
            chunkMap.put(key, result.chunk());
        }
        return scores.entrySet().stream().sorted(Map.Entry.<String, Double>comparingByValue().reversed()).limit(topK).map(entry -> new SearchResult(chunkMap.get(entry.getKey()), entry.getValue())).toList();
    }

    private void buildBm25Index() {
        invertedIndex.clear();
        docLengths = new int[chunks.size()];
        long totalLength = 0;
        for (int i = 0; i < chunks.size(); i++) {
            String[] tokens = chunks.get(i).content().toLowerCase().split("\\s+");
            docLengths[i] = tokens.length;
            totalLength += tokens.length;
            for (String token : tokens) {
                invertedIndex.computeIfAbsent(token, ignored -> new HashMap<>()).merge(i, 1, Integer::sum);
            }
        }
        avgDocLength = chunks.isEmpty() ? 1.0 : (double) totalLength / chunks.size();
    }

    private double cosineSimilarity(float[] a, float[] b) {
        if (a.length != b.length) {
            return 0;
        }
        double dot = 0, normA = 0, normB = 0;
        for (int i = 0; i < a.length; i++) {
            dot += a[i] * b[i];
            normA += a[i] * a[i];
            normB += b[i] * b[i];
        }
        double denominator = Math.sqrt(normA) * Math.sqrt(normB);
        return denominator == 0 ? 0 : dot / denominator;
    }
}
