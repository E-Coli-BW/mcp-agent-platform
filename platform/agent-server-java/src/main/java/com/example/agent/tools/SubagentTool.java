package com.example.agent.tools;

import com.example.agent.agent.SubagentContext;
import com.example.agent.agent.SubagentResult;
import com.example.agent.agent.SubagentSpawner;
import com.fasterxml.jackson.annotation.JsonProperty;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;
import java.util.Set;

/**
 * Subagent spawning tool — delegates work to a scoped child agent.
 *
 * <p>The tool schema is intentionally simple: the LLM specifies a role, brief,
 * and allowed tools, and gets back the child's answer or error message. All
 * governance (depth, fanout, budget, deadline) is handled internally by
 * {@link SubagentSpawner} and {@link SubagentContext}.</p>
 */
@Configuration
public class SubagentTool {

    private static final Logger log = LoggerFactory.getLogger(SubagentTool.class);

    private final SubagentSpawner spawner;

    /**
     * ThreadLocal holding the current SubagentContext for this request.
     * Set by the agent loop before tool execution; read by this tool.
     */
    public static final ThreadLocal<SubagentContext> CURRENT_CONTEXT = new ThreadLocal<>();

    public SubagentTool(SubagentSpawner spawner) {
        this.spawner = spawner;
    }

    @Bean
    public FunctionToolCallback<SpawnSubagentInput, String> spawnSubagent() {
        return FunctionToolCallback.<SpawnSubagentInput, String>builder(
                "spawn_subagent",
                this::handleSpawn
        )
        .description(
                "Delegate a subtask to a specialist subagent. The child agent "
                + "has access only to the tools you specify and runs with strict "
                + "budget governance. Use this for tasks that benefit from focused "
                + "execution (e.g., 'read and summarize file X', 'search memory for Y'). "
                + "Returns the child's answer or an error message."
        )
        .inputType(SpawnSubagentInput.class)
        .build();
    }

    private String handleSpawn(SpawnSubagentInput input) {
        SubagentContext ctx = CURRENT_CONTEXT.get();
        if (ctx == null) {
            // Fallback: create a permissive default (for tests / non-standard paths)
            log.warn("spawn_subagent called without SubagentContext — using permissive default");
            ctx = SubagentContext.root("adhoc", Set.copyOf(input.allowedTools()));
        }

        SubagentResult result = spawner.spawn(
                ctx,
                input.role(),
                input.brief(),
                input.allowedTools(),
                input.model(),
                input.maxToolCalls() != null ? input.maxToolCalls() : 10,
                input.maxTokens() != null ? input.maxTokens() : 8000
        );

        // Update the parent context after spawn (fanout + token settlement)
        SubagentContext updatedCtx = ctx.withFanoutIncremented()
                .withTokensConsumed(result.totalTokens());
        CURRENT_CONTEXT.set(updatedCtx);

        return result.formatForLlm();
    }

    /**
     * Input schema for the spawn_subagent tool.
     */
    public record SpawnSubagentInput(
            String role,
            String brief,
            @JsonProperty("allowed_tools") List<String> allowedTools,
            String model,
            @JsonProperty("max_tool_calls") Integer maxToolCalls,
            @JsonProperty("max_tokens") Integer maxTokens
    ) {
        public SpawnSubagentInput {
            role = role == null ? "specialist" : role;
            brief = brief == null ? "" : brief;
            allowedTools = allowedTools == null ? List.of() : List.copyOf(allowedTools);
        }
    }
}

