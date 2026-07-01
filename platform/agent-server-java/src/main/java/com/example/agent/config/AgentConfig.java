package com.example.agent.config;

import java.util.List;
import java.util.Map;

/**
 * YAML-backed agent configuration.
 */
public record AgentConfig(
        String id,
        String name,
        String version,
        String model,
        String prompt,
        List<String> tools,
        Map<String, Object> guardrails,
        Map<String, Object> routing
) {
    public AgentConfig {
        version = version == null ? "1.0" : version;
        model = model == null ? "qwen2.5:7b" : model;
        prompt = prompt == null ? "" : prompt;
        tools = tools == null ? List.of() : List.copyOf(tools);
        guardrails = guardrails == null ? Map.of() : Map.copyOf(guardrails);
        routing = routing == null ? Map.of() : Map.copyOf(routing);
    }
}
