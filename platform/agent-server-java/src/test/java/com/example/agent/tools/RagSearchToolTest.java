package com.example.agent.tools;

import com.example.agent.rag.CodeChunk;
import com.example.agent.rag.SearchResult;
import com.example.agent.workspace.WorkspaceResolver;
import com.example.mcp.common.security.TenantContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.function.Function;

import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class RagSearchToolTest {

    @TempDir
    Path workspace;

    @AfterEach
    void clearTenant() {
        TenantContext.clear();
    }

    @Test
    void should_resolveWorkspaceAndQueryIndexForAuthenticatedTenant() {
        WorkspaceIndexRegistry indexRegistry = mock(WorkspaceIndexRegistry.class);
        WorkspaceResolver workspaceResolver = mock(WorkspaceResolver.class);
        CodeChunk chunk = new CodeChunk(
                "class Monitor {}", "src/Monitor.java", "java", "class",
                "Monitor", 1, 1);
        when(workspaceResolver.forTenant("tenant-a")).thenReturn(workspace);
        when(indexRegistry.search("tenant-a", workspace, "monitor", 5)).thenReturn(
                new WorkspaceIndexRegistry.SearchOutcome(
                        List.of(new SearchResult(chunk, 0.9)), null));
        Function<RagSearchTool.RagSearchRequest, String> search =
                new RagSearchTool(indexRegistry, workspaceResolver).rag_search();
        TenantContext.set("tenant-a");

        String result = search.apply(new RagSearchTool.RagSearchRequest("monitor", null));

        assertTrue(result.contains("src/Monitor.java:1"));
        verify(workspaceResolver).forTenant("tenant-a");
        verify(indexRegistry).search("tenant-a", workspace, "monitor", 5);
    }

    @Test
    void should_returnUnavailableReasonFromScopedIndex() {
        WorkspaceIndexRegistry indexRegistry = mock(WorkspaceIndexRegistry.class);
        WorkspaceResolver workspaceResolver = mock(WorkspaceResolver.class);
        when(workspaceResolver.forTenant("tenant-a")).thenReturn(workspace);
        when(indexRegistry.search("tenant-a", workspace, "monitor", 3)).thenReturn(
                WorkspaceIndexRegistry.SearchOutcome.unavailable("workspace is unreadable"));
        Function<RagSearchTool.RagSearchRequest, String> search =
                new RagSearchTool(indexRegistry, workspaceResolver).rag_search();
        TenantContext.set("tenant-a");

        String result = search.apply(new RagSearchTool.RagSearchRequest("monitor", 3));

        assertTrue(result.contains("workspace is unreadable"));
    }
}
