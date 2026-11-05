package com.example.agent.tools;

import com.example.agent.rag.SearchResult;
import com.example.agent.workspace.WorkspaceResolver;
import com.example.mcp.common.security.TenantContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.nio.file.Path;
import java.util.List;
import java.util.function.Function;

/**
 * Exposes tenant-scoped RAG search over the active workspace.
 */
@Configuration
public class RagSearchTool {

    private final WorkspaceIndexRegistry indexRegistry;
    private final WorkspaceResolver workspaceResolver;

    public RagSearchTool(
            WorkspaceIndexRegistry indexRegistry,
            WorkspaceResolver workspaceResolver) {
        this.indexRegistry = indexRegistry;
        this.workspaceResolver = workspaceResolver;
    }

    @Bean
    public Function<RagSearchRequest, String> rag_search() {
        return request -> {
            String tenantId = TenantContext.get();
            Path workspace = workspaceResolver.forTenant(tenantId);
            int topK = request.topK() != null ? request.topK() : 5;
            WorkspaceIndexRegistry.SearchOutcome outcome = indexRegistry.search(
                    tenantId, workspace, request.query(), topK);
            if (outcome.unavailableMessage() != null) {
                return outcome.unavailableMessage();
            }
            List<SearchResult> results = outcome.results();
            if (results.isEmpty()) {
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
                        .append(result.chunk().content(), 0,
                                Math.min(200, result.chunk().content().length()))
                        .append("\n\n");
            }
            return builder.toString();
        };
    }

    public record RagSearchRequest(String query, Integer topK) {
    }
}
