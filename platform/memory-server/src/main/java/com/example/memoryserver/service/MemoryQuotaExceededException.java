package com.example.memoryserver.service;

/**
 * Thrown when a tenant exceeds the maximum number of memory entries.
 */
public class MemoryQuotaExceededException extends RuntimeException {
    public MemoryQuotaExceededException(String tenantId, int maxEntries) {
        super(String.format("Tenant '%s' has reached the maximum of %d memory entries", tenantId, maxEntries));
    }
}
