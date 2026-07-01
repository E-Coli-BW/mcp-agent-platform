package com.example.memoryserver.model.dto;

import com.example.memoryserver.model.MemoryEntity;

import java.time.Instant;
import java.util.Set;

/**
 * Output DTO for memory responses.
 * Hides internal fields (id, version) from API consumers.
 */
public record MemoryResponse(
    String key,
    String content,
    String namespace,
    Set<String> tags,
    Instant createdAt,
    Instant updatedAt,
    Instant lastAccessedAt,
    int accessCount,
    boolean pinned
) {
    /** Convert from JPA entity. */
    public static MemoryResponse from(MemoryEntity entity) {
        return new MemoryResponse(
            entity.getKey(),
            entity.getContent(),
            entity.getNamespace(),
            entity.getTags(),
            entity.getCreatedAt(),
            entity.getUpdatedAt(),
            entity.getLastAccessedAt(),
            entity.getAccessCount(),
            entity.isPinned()
        );
    }
}
