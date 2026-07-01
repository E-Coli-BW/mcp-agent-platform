package com.example.memoryserver.model.dto;

import java.util.Set;

/**
 * Input DTO for memory_set tool.
 * Decouples MCP tool input from JPA entity.
 */
public record MemoryRequest(
    String key,
    String content,
    String namespace,
    Set<String> tags,
    Boolean pinned
) {
    /** Create with defaults for optional fields. */
    public static MemoryRequest of(String key, String content) {
        return new MemoryRequest(key, content, "default", Set.of(), false);
    }

    /** Resolve namespace with fallback. */
    public String resolvedNamespace(String fallback) {
        return (namespace != null && !namespace.isEmpty()) ? namespace : fallback;
    }
}
