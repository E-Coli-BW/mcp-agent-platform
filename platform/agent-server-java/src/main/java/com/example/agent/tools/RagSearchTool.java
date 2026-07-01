package com.example.agent.tools;

import com.example.agent.rag.HybridRetriever;
import com.example.agent.rag.SearchResult;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;
import java.util.function.Function;

/**
 * RAG search tool backed by the hybrid retriever.
 */
@Configuration
public class RagSearchTool {

    private final HybridRetriever retriever;

    public RagSearchTool(HybridRetriever retriever) {
        this.retriever = retriever;
    }

    /**
     * Exposes the rag_search function callback.
     */
    @Bean
    public Function<RagSearchRequest, String> rag_search() {
        return request -> {
            if (!retriever.hasIndex()) {
                return "No RAG index found for this workspace. Use file_search instead.";
            }
            int topK = request.topK() != null ? request.topK() : 5;
            List<SearchResult> results = retriever.search(request.query(), topK).block();
            if (results == null || results.isEmpty()) {
                return "No relevant code found for: " + request.query();
            }
            StringBuilder builder = new StringBuilder();
            for (SearchResult result : results) {
                builder.append(result.chunk().filePath())
                        .append(":")
                        .append(result.chunk().startLine())
                        .append(" — ")
                        .append(result.chunk().name())
                        .append(" (")
                        .append(String.format("%.2f", result.score()))
                        .append(")\n")
                        .append(result.chunk().content(), 0, Math.min(200, result.chunk().content().length()))
                        .append("\n\n");
            }
            return builder.toString();
        };
    }

    public record RagSearchRequest(String query, Integer topK) {
    }
}
