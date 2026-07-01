package com.example.agent.governance;

/**
 * Resolves effective system prompt by governance policy.
 */
public interface PromptResolver {

    PromptResolution resolve(String tenantId, String sessionId, String requestedVersion);
}

