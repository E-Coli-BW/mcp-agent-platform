package com.example.agent.tools;

import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.ToolCallbackProvider;
import org.springframework.context.ApplicationContext;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Registry of all Spring AI tool callbacks available to the agent.
 *
 * <p>Uses lazy initialization via @PostConstruct to avoid circular dependency
 * issues (SubagentTool → SubagentSpawner → AgentFactory → ToolRegistry).</p>
 */
@Component
public class ToolRegistry {

    private static final Logger log = LoggerFactory.getLogger(ToolRegistry.class);

    private final ApplicationContext applicationContext;
    private volatile Map<String, ToolCallback> toolMap;

    public ToolRegistry(ApplicationContext applicationContext) {
        this.applicationContext = applicationContext;
    }

    /**
     * Collect all ToolCallback beans after the context is fully initialized.
     * This avoids circular dependency issues since beans are already created.
     */
    @PostConstruct
    void initialize() {
        LinkedHashMap<String, ToolCallback> callbacks = new LinkedHashMap<>();

        // Collect from ToolCallbackProvider beans (if any)
        Map<String, ToolCallbackProvider> providers = applicationContext.getBeansOfType(ToolCallbackProvider.class);
        for (ToolCallbackProvider provider : providers.values()) {
            for (ToolCallback callback : provider.getToolCallbacks()) {
                String name = callback.getToolDefinition().name();
                callbacks.putIfAbsent(name, callback);
            }
        }

        // Collect individually registered ToolCallback beans
        Map<String, ToolCallback> individualBeans = applicationContext.getBeansOfType(ToolCallback.class);
        for (ToolCallback callback : individualBeans.values()) {
            String name = callback.getToolDefinition().name();
            callbacks.putIfAbsent(name, callback);
        }

        this.toolMap = Map.copyOf(callbacks);
        log.info("✅ ToolRegistry initialized with {} tools: {}",
                this.toolMap.size(), this.toolMap.keySet());
    }

    /**
     * Resolve a single tool callback by name.
     *
     * @param name tool name
     * @return matching callback if present
     */
    public Optional<ToolCallback> getToolCallback(String name) {
        return Optional.ofNullable(this.toolMap.get(name));
    }

    /**
     * Resolve the requested tools by name.
     *
     * @param names tool names to resolve
     * @return matching callbacks in request order
     */
    public List<ToolCallback> resolveTools(List<String> names) {
        return names.stream()
                .map(this.toolMap::get)
                .filter(java.util.Objects::nonNull)
                .toList();
    }

    /**
     * Return every registered tool callback.
     *
     * @return all callbacks as a list
     */
    public List<ToolCallback> getAllTools() {
        return List.copyOf(this.toolMap.values());
    }

    /**
     * Return every registered tool callback as an array.
     *
     * @return all callbacks as an array
     */
    public ToolCallback[] getAllToolCallbacks() {
        return this.toolMap.values().toArray(ToolCallback[]::new);
    }

    /**
     * Return the number of registered tools.
     *
     * @return tool count
     */
    public int size() {
        return this.toolMap.size();
    }
}
