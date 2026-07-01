package com.example.memoryserver.tool;

import com.example.mcp.common.security.TenantContext;
import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.model.dto.MemoryRequest;
import com.example.memoryserver.model.dto.MemoryResponse;
import com.example.memoryserver.search.MemorySearchEngine.ScoredResult;
import com.example.memoryserver.service.MemoryService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;

/**
 * MCP Tool definitions — thin adapter between MCP protocol and MemoryService.
 * Each method extracts tenant context, delegates to MemoryService, and formats the response.
 */
@Service
public class MemoryToolService {

    private final MemoryService memoryService;
    private final ObjectMapper mapper;

    public MemoryToolService(MemoryService memoryService, ObjectMapper mapper) {
        this.memoryService = memoryService;
        this.mapper = mapper;
    }

    @Tool(description = "Save information to persistent memory. Use to remember facts, decisions, preferences.")
    public String memory_set(
            @ToolParam(description = "Unique identifier for this memory") String key,
            @ToolParam(description = "The content to remember") String content,
            @ToolParam(description = "Namespace to organize memories (default: 'default')", required = false) String namespace,
            @ToolParam(description = "Tags for categorization", required = false) List<String> tags,
            @ToolParam(description = "Pin to protect from forgetting", required = false) Boolean pinned) {
        String validatedKey = requireNonBlank(key, "key");
        String validatedContent = requireNonBlank(content, "content");
        String tid = TenantContext.get();
        var request = new MemoryRequest(validatedKey, validatedContent, namespace,
                tags != null ? new HashSet<>(tags) : null, pinned);
        MemoryEntity entity = memoryService.set(tid, request);
        String pin = entity.isPinned() ? " 📌" : "";
        return String.format("✅ Memory saved: \"%s\" (namespace: %s)%s", validatedKey, entity.getNamespace(), pin);
    }

    @Tool(description = "Retrieve a specific memory by key. Returns full content and metadata.")
    public String memory_get(@ToolParam(description = "The key of the memory to retrieve") String key) {
        String validatedKey = requireNonBlank(key, "key");
        String tid = TenantContext.get();
        return memoryService.get(tid, validatedKey)
                .map(e -> toJson(MemoryResponse.from(e)))
                .orElse("❌ Memory not found: \"" + validatedKey + "\"");
    }

    @Tool(description = "Search memories by keywords and/or tags. Returns ranked results.")
    public String memory_search(
            @ToolParam(description = "Natural language search query") String query,
            @ToolParam(description = "Filter by namespace", required = false) String namespace,
            @ToolParam(description = "Filter by tags", required = false) List<String> tags,
            @ToolParam(description = "Max results (default: 10)", required = false) Integer limit) {
        String validatedQuery = requireNonBlank(query, "query");
        int resolvedLimit = resolveLimit(limit);
        String tid = TenantContext.get();
        List<ScoredResult> results = memoryService.search(tid, validatedQuery, tags, namespace, resolvedLimit);

        if (results.isEmpty()) {
            String ns = namespace != null ? " in namespace \"" + namespace + "\"" : "";
            return "🔍 No memories found matching \"" + validatedQuery + "\"" + ns;
        }

        var items = new ArrayList<Map<String, Object>>();
        for (int i = 0; i < results.size(); i++) {
            var r = results.get(i);
            String content = r.entity().getContent();
            if (content.length() > 200) {
                content = content.substring(0, 200) + "...";
            }
            items.add(Map.of(
                    "rank", i + 1,
                    "key", r.entity().getKey(),
                    "content", content,
                    "namespace", r.entity().getNamespace(),
                    "score", Math.round(r.score() * 100.0) / 100.0));
        }
        return "🔍 Found " + results.size() + " result(s) for \"" + validatedQuery + "\":\n\n" + toJson(items);
    }

    @Tool(description = "Delete a memory entry by key.")
    public String memory_delete(@ToolParam(description = "The key to delete") String key) {
        String validatedKey = requireNonBlank(key, "key");
        String tid = TenantContext.get();
        boolean deleted = memoryService.delete(tid, validatedKey);
        return deleted ? "🗑️ Memory deleted: \"" + validatedKey + "\"" : "❌ Memory not found: \"" + validatedKey + "\"";
    }

    @Tool(description = "Pin or unpin a memory. Pinned memories are immune to forgetting.")
    public String memory_pin(
            @ToolParam(description = "The key to pin/unpin") String key,
            @ToolParam(description = "true to pin, false to unpin", required = false) Boolean pinned) {
        String validatedKey = requireNonBlank(key, "key");
        String tid = TenantContext.get();
        boolean pin = pinned != null ? pinned : true;
        return memoryService.pin(tid, validatedKey, pin)
                .map(e -> pin
                        ? "📌 Memory pinned: \"" + validatedKey + "\" — will never be auto-forgotten"
                        : "📌 Memory unpinned: \"" + validatedKey + "\"")
                .orElse("❌ Memory not found: \"" + validatedKey + "\"");
    }

    @Tool(description = "List memories, optionally filtered by namespace or tags.")
    public String memory_list(
            @ToolParam(description = "Filter by namespace", required = false) String namespace,
            @ToolParam(description = "Filter by tags", required = false) List<String> tags) {
        String tid = TenantContext.get();
        List<MemoryEntity> entries = memoryService.list(tid, namespace,
                tags != null ? new HashSet<>(tags) : null);

        if (entries.isEmpty()) {
            return "📭 No memories found.";
        }

        var summary = entries.stream()
                .sorted((a, b) -> b.getUpdatedAt().compareTo(a.getUpdatedAt()))
                .map(e -> Map.of(
                        "key", (Object) e.getKey(),
                        "namespace", e.getNamespace(),
                        "tags", e.getTags(),
                        "preview", e.getContent().length() > 80
                                ? e.getContent().substring(0, 80) + "..." : e.getContent(),
                        "pinned", e.isPinned()))
                .toList();

        return "📋 " + entries.size() + " memory/memories:\n\n" + toJson(summary);
    }

    @Tool(description = "Get memory system overview. Use at session start to understand available context.")
    public String memory_context() {
        String tid = TenantContext.get();
        return memoryService.context(tid);
    }

    private String requireNonBlank(String value, String fieldName) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(fieldName + " is required");
        }
        return value;
    }

    private int resolveLimit(Integer limit) {
        if (limit == null) {
            return 10;
        }
        if (limit < 1) {
            throw new IllegalArgumentException("limit must be greater than 0");
        }
        return limit;
    }

    private String toJson(Object obj) {
        try {
            return mapper.writerWithDefaultPrettyPrinter().writeValueAsString(obj);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to serialize tool response", e);
        }
    }
}
