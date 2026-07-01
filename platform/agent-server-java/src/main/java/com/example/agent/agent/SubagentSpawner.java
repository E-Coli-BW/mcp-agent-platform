package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import com.example.agent.tools.ToolRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.messages.Message;
import org.springframework.ai.chat.messages.SystemMessage;
import org.springframework.ai.chat.messages.ToolResponseMessage;
import org.springframework.ai.chat.messages.UserMessage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.ollama.api.OllamaChatOptions;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

/**
 * Subagent spawner — invoke a scoped subagent in-process.
 *
 * <p>Design: spawn the child agent IN-PROCESS, not over HTTP. The child inherits
 * the parent's tenant context, uses a narrowed tool set, and runs under strict
 * budget governance from {@link SubagentContext}.</p>
 *
 * <p>Each spawn builds an ephemeral agent (NOT cached). A subagent needs a narrowed
 * tool list, so we can't reuse the parent's cached model. We DO reuse the ChatModel
 * factory (which is cheap).</p>
 */
@Service
public class SubagentSpawner {

    private static final Logger log = LoggerFactory.getLogger(SubagentSpawner.class);

    private final AgentFactory agentFactory;
    private final ToolRegistry toolRegistry;
    private final FleetBus fleetBus;
    private final AgentProperties properties;

    public SubagentSpawner(AgentFactory agentFactory, ToolRegistry toolRegistry,
                           FleetBus fleetBus, AgentProperties properties) {
        this.agentFactory = agentFactory;
        this.toolRegistry = toolRegistry;
        this.fleetBus = fleetBus;
        this.properties = properties;
    }

    /**
     * Spawn a scoped subagent to answer {@code brief} and return its result.
     *
     * @param parentContext the parent's SubagentContext (for budget enforcement)
     * @param role short label for logs/dashboard
     * @param brief full task description (becomes the child's user message)
     * @param allowedTools tool name allowlist (must be subset of parent's)
     * @param model model override (null = same as parent)
     * @param maxToolCalls internal step limit for the child
     * @param maxTokens token estimate for budget reservation
     * @return SubagentResult — never throws on happy path
     */
    public SubagentResult spawn(SubagentContext parentContext, String role, String brief,
                                List<String> allowedTools, String model,
                                int maxToolCalls, int maxTokens) {

        long startMs = System.currentTimeMillis();
        String childSessionId = parentContext.rootSessionId() + "/sub-" + UUID.randomUUID().toString().substring(0, 8);

        // Phase 1: gate-keep at the budget envelope
        SubagentContext childContext;
        try {
            childContext = parentContext.deriveChild(
                    childSessionId,
                    Set.copyOf(allowedTools),
                    maxTokens
            );
        } catch (SubagentContext.SpawnRejectedException e) {
            log.warn("❌ spawn_subagent rejected (role={}, depth={}): {}",
                    role, parentContext.depth(), e.getMessage());
            return SubagentResult.failed(childSessionId, role, parentContext.depth() + 1,
                    e.getMessage(), System.currentTimeMillis() - startMs);
        }

        log.info("🤖 spawn_subagent role={} depth={} session={} tools={} budget={}",
                role, childContext.depth(), childSessionId, allowedTools, childContext.tokensRemaining());

        // Phase 2: build the child agent with narrowed tool set
        ToolCallback[] childTools;
        try {
            List<ToolCallback> resolved = toolRegistry.resolveTools(allowedTools);
            childTools = resolved.toArray(ToolCallback[]::new);
        } catch (Exception e) {
            log.error("spawn_subagent build failed for role={}: {}", role, e.getMessage());
            return SubagentResult.failed(childSessionId, role, childContext.depth(),
                    "failed to construct subagent: " + e.getMessage(),
                    System.currentTimeMillis() - startMs);
        }

        ChatModel chatModel = agentFactory.getChatModel(model, null);

        // Phase 3: run the child
        // Publish child_start
        fleetBus.publishEvent(parentContext.rootSessionId(), childSessionId, role,
                "child_start", Map.of(
                        "depth", childContext.depth(),
                        "brief_preview", brief.length() > 200 ? brief.substring(0, 200) : brief
                ));

        SubagentResult result = runChild(chatModel, childTools, brief, childSessionId,
                role, childContext.depth(), maxToolCalls, childContext.remainingMs(),
                parentContext.rootSessionId());

        result = result.withDurationMs(System.currentTimeMillis() - startMs);

        // Publish child_end or child_cancelled
        String eventType = "cancelled by parent".equals(result.error()) ? "child_cancelled" : "child_end";
        fleetBus.publishEvent(parentContext.rootSessionId(), childSessionId, role,
                eventType, Map.of(
                        "answer_preview", result.answer() != null
                                ? result.answer().substring(0, Math.min(200, result.answer().length())) : "",
                        "tokens", result.totalTokens(),
                        "tool_names", result.toolNames(),
                        "error", result.error() != null ? result.error() : ""
                ));

        return result;
    }

    /**
     * Drive the child agent's ReAct loop with a deadline.
     */
    private SubagentResult runChild(ChatModel chatModel, ToolCallback[] tools, String brief,
                                     String childSessionId, String role, int depth,
                                     int maxToolCalls, long deadlineMs, String rootSessionId) {

        String systemPrompt = buildSubagentSystemPrompt(role, maxToolCalls);
        List<Message> messages = new ArrayList<>();
        messages.add(new UserMessage(brief));
        List<String> toolNames = new ArrayList<>();
        StringBuilder answer = new StringBuilder();
        int promptTokens = 0;
        int completionTokens = 0;
        int loopCounter = 0;
        long timeoutMs = Math.max(1000, deadlineMs);

        long deadline = System.currentTimeMillis() + timeoutMs;

        while (loopCounter < maxToolCalls) {
            loopCounter++;

            // Check deadline
            if (System.currentTimeMillis() > deadline) {
                return SubagentResult.of(childSessionId, role, answer.toString().trim(),
                        toolNames, promptTokens, completionTokens, depth,
                        "subagent exceeded wallclock deadline of " + deadlineMs + "ms");
            }

            // Check cancellation
            if (fleetBus.isCancelled(rootSessionId, childSessionId)) {
                return SubagentResult.of(childSessionId, role, answer.toString().trim(),
                        toolNames, promptTokens, completionTokens, depth,
                        "cancelled by parent");
            }

            // Build prompt
            List<Message> fullMessages = new ArrayList<>();
            fullMessages.add(new SystemMessage(systemPrompt));
            fullMessages.addAll(messages);

            // Estimate prompt tokens
            for (Message msg : fullMessages) {
                String text = msg.getText();
                if (text != null) {
                    promptTokens += text.length() / 4;
                }
            }

            Prompt prompt = new Prompt(fullMessages, OllamaChatOptions.builder()
                    .toolCallbacks(java.util.Arrays.asList(tools))
                    .internalToolExecutionEnabled(false)
                    .build());

            // Call LLM
            try {
                long remainingMs = deadline - System.currentTimeMillis();
                if (remainingMs <= 0) break;

                ChatResponse response = chatModel.call(prompt);
                if (response == null || response.getResults() == null || response.getResults().isEmpty()) {
                    break;
                }

                Generation generation = response.getResults().get(0);
                AssistantMessage output = generation.getOutput();

                // Collect text content
                String text = output.getText();
                if (text != null && !text.isEmpty()) {
                    answer.append(text);
                    completionTokens += text.length() / 4;
                    // Publish tokens to fleet bus
                    fleetBus.publishEvent(rootSessionId, childSessionId, role,
                            "child_token", Map.of("token", text));
                }

                // Check for tool calls
                if (output.getToolCalls() != null && !output.getToolCalls().isEmpty()) {
                    messages.add(output);

                    List<ToolResponseMessage.ToolResponse> toolResponses = new ArrayList<>();
                    for (AssistantMessage.ToolCall toolCall : output.getToolCalls()) {
                        String toolName = toolCall.name();
                        toolNames.add(toolName);

                        fleetBus.publishEvent(rootSessionId, childSessionId, role,
                                "child_tool_start", Map.of("tool", toolName));

                        // Execute tool
                        String toolResult;
                        try {
                            toolResult = executeTool(toolName, toolCall.arguments(), tools);
                        } catch (Exception e) {
                            toolResult = "❌ Tool execution failed: " + e.getMessage();
                        }

                        // Truncate
                        int maxOutput = properties.maxToolOutput();
                        if (toolResult.length() > maxOutput) {
                            toolResult = toolResult.substring(0, maxOutput) + "\n... (truncated)";
                        }

                        fleetBus.publishEvent(rootSessionId, childSessionId, role,
                                "child_tool_end", Map.of(
                                        "tool", toolName,
                                        "output_preview", toolResult.substring(0, Math.min(200, toolResult.length()))
                                ));

                        toolResponses.add(new ToolResponseMessage.ToolResponse(
                                toolCall.id(), toolName, toolResult));
                    }
                    messages.add(ToolResponseMessage.builder().responses(toolResponses).build());
                    // Continue loop — LLM will see tool results next round
                } else {
                    // No tool calls — child is done
                    break;
                }
            } catch (Exception e) {
                log.error("subagent role={} crashed: {}", role, e.getMessage());
                return SubagentResult.of(childSessionId, role, answer.toString().trim(),
                        toolNames, promptTokens, completionTokens, depth,
                        "subagent crashed: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        }

        return SubagentResult.of(childSessionId, role, answer.toString().trim(),
                toolNames, promptTokens, completionTokens, depth, null);
    }

    private String executeTool(String toolName, String arguments, ToolCallback[] tools) {
        for (ToolCallback tool : tools) {
            if (tool.getToolDefinition().name().equals(toolName)) {
                return tool.call(arguments);
            }
        }
        return "❌ Unknown tool: " + toolName;
    }

    private String buildSubagentSystemPrompt(String role, int maxToolCalls) {
        return "You are a specialist subagent acting as: " + role + ".\n"
                + "\n"
                + "Your task is given in the user message below. Complete it using\n"
                + "the tools you have, then return a SHORT direct answer (1-3\n"
                + "paragraphs). Your answer is consumed by another agent, not a\n"
                + "human — be terse and information-dense, no pleasantries.\n"
                + "\n"
                + "RULES:\n"
                + "- Hard limit: " + maxToolCalls + " tool calls. Plan accordingly.\n"
                + "- DO NOT spawn more subagents. You are a leaf worker.\n"
                + "- DO NOT ask clarifying questions — make your best inference.\n"
                + "- If the task is impossible or out of scope, say so in one\n"
                + "  sentence and explain why.\n"
                + "\n"
                + "VERBATIM QUOTING (CRITICAL):\n"
                + "- If the task asks for an identifier (class name, function name,\n"
                + "  constant), QUOTE IT VERBATIM from the tool output.\n"
                + "- Do not normalize capitalization or 'improve' the name.\n"
                + "- If you cannot find the identifier, say so explicitly.\n";
    }
}





