package com.example.agent.agent;

import java.util.List;

/**
 * What a single subagent spawn produced — structured for logs and observability.
 *
 * <p>The LLM that called spawn_subagent only sees {@link #formatForLlm()};
 * everything else is for the dashboard, eval harness, and audit log.</p>
 */
public record SubagentResult(
        String childSessionId,
        String role,
        String answer,
        List<String> toolNames,
        int promptTokens,
        int completionTokens,
        long durationMs,
        int depth,
        String error,
        Boolean verified,
        Integer verifierGrade,
        String verifierReasoning,
        boolean verifierRetried
) {

    /**
     * Create a successful or error result.
     */
    public static SubagentResult of(String childSessionId, String role, String answer,
                                     List<String> toolNames, int promptTokens, int completionTokens,
                                     int depth, String error) {
        return new SubagentResult(childSessionId, role, answer, List.copyOf(toolNames),
                promptTokens, completionTokens, 0, depth, error,
                null, null, "", false);
    }

    /**
     * Create a failed result (policy rejection or build failure).
     */
    public static SubagentResult failed(String childSessionId, String role, int depth,
                                         String error, long durationMs) {
        return new SubagentResult(childSessionId, role, "", List.of(),
                0, 0, durationMs, depth, error,
                null, null, "", false);
    }

    /**
     * Return a new result with duration updated.
     */
    public SubagentResult withDurationMs(long ms) {
        return new SubagentResult(childSessionId, role, answer, toolNames,
                promptTokens, completionTokens, ms, depth, error,
                verified, verifierGrade, verifierReasoning, verifierRetried);
    }

    /**
     * Total tokens (prompt + completion).
     */
    public int totalTokens() {
        return promptTokens + completionTokens;
    }

    /**
     * Format for the parent LLM to see.
     * Kept terse — the parent reads for content, not for traces.
     */
    public String formatForLlm() {
        if (error != null && !error.isEmpty()) {
            return "❌ subagent [" + role + "] failed after "
                    + durationMs + "ms: " + error + "\n(no answer produced)";
        }
        String toolSummary = toolNames.isEmpty() ? "no tools" : String.join(", ", toolNames);
        return "✅ subagent [" + role + "] finished in "
                + durationMs + "ms (" + totalTokens() + " tokens, used: " + toolSummary + ")\n\n"
                + "--- subagent answer ---\n" + answer;
    }
}

