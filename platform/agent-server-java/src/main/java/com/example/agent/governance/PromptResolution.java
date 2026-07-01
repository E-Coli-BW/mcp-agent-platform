package com.example.agent.governance;

/**
 * Resolved system prompt metadata for governance and provenance.
 */
public record PromptResolution(
        String promptId,
        String version,
        String content,
        String contentHash,
        String assignmentSource,
        String rolloutPolicyId
) {
}

