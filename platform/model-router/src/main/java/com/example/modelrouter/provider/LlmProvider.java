package com.example.modelrouter.provider;

/**
 * SPI for LLM providers.
 * Same pattern as MemoryRepository — pluggable implementations.
 */
public interface LlmProvider {

    /** Provider name (e.g., "openai", "anthropic", "ollama") */
    String name();

    /** Check if provider is configured and reachable */
    boolean isAvailable();

    /** Execute a completion request */
    LlmResponse complete(LlmRequest request);

    /** Estimate cost before calling (optional) */
    default CostEstimate estimateCost(LlmRequest request) {
        return CostEstimate.unknown();
    }
}
