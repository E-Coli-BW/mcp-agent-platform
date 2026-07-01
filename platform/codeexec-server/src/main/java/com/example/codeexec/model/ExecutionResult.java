package com.example.codeexec.model;

import java.time.Instant;

/**
 * Result of a code execution.
 */
public record ExecutionResult(
        String stdout,
        String stderr,
        int exitCode,
        long durationMs,
        String language,
        boolean timedOut,
        Instant executedAt
) {
    public static ExecutionResult success(String stdout, String stderr, int exitCode, long durationMs, String language) {
        return new ExecutionResult(stdout, stderr, exitCode, durationMs, language, false, Instant.now());
    }

    public static ExecutionResult timeout(String partialStdout, long durationMs, String language) {
        return new ExecutionResult(partialStdout, "Execution timed out after " + durationMs + "ms",
                -1, durationMs, language, true, Instant.now());
    }

    public static ExecutionResult error(String message, String language) {
        return new ExecutionResult("", message, -1, 0, language, false, Instant.now());
    }
}
