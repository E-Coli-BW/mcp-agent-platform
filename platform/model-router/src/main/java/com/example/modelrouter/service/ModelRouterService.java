package com.example.modelrouter.service;

import com.example.modelrouter.provider.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * Routes LLM requests to the best available provider.
 *
 * Selection strategy:
 * 1. If request specifies a provider → use it (or fail)
 * 2. Otherwise → try default provider, fall back through chain
 *
 * Future: task-based routing (summarize → cheap model, explain → strong model)
 */
@Service
public class ModelRouterService {

    private static final Logger log = LoggerFactory.getLogger(ModelRouterService.class);

    private final Map<String, LlmProvider> providers;
    private final String defaultProvider;

    public ModelRouterService(
            List<LlmProvider> providerList,
            @Value("${model-router.default-provider:ollama}") String defaultProvider) {
        this.providers = providerList.stream()
                .collect(Collectors.toMap(LlmProvider::name, p -> p));
        this.defaultProvider = defaultProvider;
        log.info("ModelRouter initialized with {} providers: {}", providers.size(), providers.keySet());
    }

    /**
     * Route a completion request to the best provider.
     */
    public LlmResponse complete(LlmRequest request, String preferredProvider) {
        // 1. Try preferred provider if specified
        if (preferredProvider != null && !preferredProvider.isBlank()) {
            LlmProvider provider = providers.get(preferredProvider);
            if (provider != null && provider.isAvailable()) {
                log.info("Using preferred provider: {}", preferredProvider);
                return provider.complete(request);
            }
            log.warn("Preferred provider '{}' not available, falling back", preferredProvider);
        }

        // 2. Try default provider
        LlmProvider defaultProv = providers.get(defaultProvider);
        if (defaultProv != null && defaultProv.isAvailable()) {
            log.info("Using default provider: {}", defaultProvider);
            return defaultProv.complete(request);
        }

        // 3. Try any available provider
        for (LlmProvider provider : providers.values()) {
            if (provider.isAvailable()) {
                log.info("Falling back to provider: {}", provider.name());
                return provider.complete(request);
            }
        }

        return LlmResponse.error("No LLM providers available", "none");
    }

    /**
     * List all providers and their status.
     */
    public List<Map<String, Object>> listModels() {
        return providers.values().stream()
                .map(p -> Map.<String, Object>of(
                        "provider", p.name(),
                        "available", p.isAvailable(),
                        "cost", p.estimateCost(LlmRequest.of("test")).toString()
                ))
                .toList();
    }
}
