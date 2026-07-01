package com.example.memoryserver.service;

/**
 * Thrown when a write operation fails due to concurrent modification
 * (optimistic lock conflict or duplicate key race condition).
 */
public class MemoryConflictException extends RuntimeException {
    public MemoryConflictException(String message, Throwable cause) {
        super(message, cause);
    }
}
