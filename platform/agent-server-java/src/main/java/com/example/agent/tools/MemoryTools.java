package com.example.agent.tools;

import com.example.agent.config.AgentProperties;
import com.example.mcp.common.security.TenantContext;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.lang.Nullable;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Configuration
public class MemoryTools {

    private final McpRestClient memoryClient;

    public MemoryTools(AgentProperties agentProperties, @Nullable AuthServiceClient authClient) {
        this.memoryClient = new McpRestClient(
            agentProperties.memoryServerUrl(),
            agentProperties.jwtSecret(),
            Duration.ofSeconds(10),
            authClient,
            "memory-server"
        );
    }

    @Bean
    public FunctionToolCallback<MemorySearchInput, String> memorySearch() {
        return FunctionToolCallback.<MemorySearchInput, String>builder(
                "memory_search",
                (MemorySearchInput input) -> memorySearch(input)
            )
            .description("Search persistent memory for relevant context.")
            .inputType(MemorySearchInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<MemorySetInput, String> memorySet() {
        return FunctionToolCallback.<MemorySetInput, String>builder(
                "memory_set",
                (MemorySetInput input) -> memorySet(input)
            )
            .description("Save important information to persistent memory.")
            .inputType(MemorySetInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<NoInput, String> memoryContext() {
        return FunctionToolCallback.<NoInput, String>builder(
                "memory_context",
                (NoInput input) -> memoryContext(input)
            )
            .description("Get an overview of stored memory context.")
            .inputType(NoInput.class)
            .build();
    }

    private String memorySearch(MemorySearchInput input) {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("query", input.query());
        if (input.namespace() != null && !input.namespace().isBlank()) {
            args.put("namespace", input.namespace());
        }
        return call("memory_search", args);
    }

    private String memorySet(MemorySetInput input) {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("key", input.key());
        args.put("content", input.content());
        if (input.tags() != null && !input.tags().isEmpty()) {
            args.put("tags", input.tags());
        }
        return call("memory_set", args);
    }

    private String memoryContext(NoInput ignored) {
        return call("memory_context", Map.of());
    }

    private String call(String toolName, Map<String, Object> args) {
        return memoryClient.callTool(toolName, args, TenantContext.getOrNull())
            .blockOptional()
            .orElse("❌ Service unavailable: " + toolName);
    }

    public record NoInput() {}

    public record MemorySearchInput(String query, String namespace) {}

    public record MemorySetInput(String key, String content, List<String> tags) {}
}
