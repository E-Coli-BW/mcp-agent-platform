package com.example.agent.agent;

import com.example.agent.config.AgentProperties;
import com.example.agent.governance.ModelCallProvenance;
import com.example.agent.governance.ProvenanceEmitter;
import com.example.agent.governance.PromptResolution;
import com.example.agent.governance.PromptResolver;
import com.example.agent.streaming.SseEventBuilder;
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
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Flux;
import reactor.core.publisher.FluxSink;
import reactor.core.scheduler.Schedulers;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Streaming ReAct agent loop — the brain of the Java agent server.
 *
 * <p>Replaces the blocking {@code ChatClient.prompt().call()} with an explicit
 * streaming loop that mirrors the Python LangGraph v2 architecture:</p>
 * <pre>
 *   compress_context → call_llm (stream tokens) → detect_tool_calls → execute_tools
 *                                                                      → track_errors
 *                                                                      → compress_context
 *                                                                      → call_llm → ...
 *                                                 → no tool_calls → END
 * </pre>
 *
 * <p>Key behaviors:</p>
 * <ul>
 *   <li>True per-token streaming: SSE events emitted as the LLM generates each chunk</li>
 *   <li>Context window management: old tool messages compressed before each LLM call</li>
 *   <li>Error recovery: consecutive errors tracked with retry hints</li>
 *   <li>Step limit: hard cap at {@code maxAgentSteps} to prevent infinite loops</li>
 *   <li>Tool output truncation: defense-in-depth per-tool size limit</li>
 * </ul>
 */
@Service
public class AgentLoopService {

    private static final Logger log = LoggerFactory.getLogger(AgentLoopService.class);

    /**
     * Thread pool for parallel tool execution. Sized to MAX_FANOUT_CEILING (8)
     * because that's the most concurrent subagent spawns we'll ever allow.
     * VirtualThread executor would be ideal here (JDK 21) for I/O-bound tool calls.
     */
    private static final ExecutorService TOOL_EXECUTOR = Executors.newVirtualThreadPerTaskExecutor();

    private final AgentFactory agentFactory;
    private final ContextCompressor contextCompressor;
    private final AgentProperties properties;
    private final PromptResolver promptResolver;
    private final PromptService promptService;
    private final ProvenanceEmitter provenanceEmitter;

    public AgentLoopService(AgentFactory agentFactory, ContextCompressor contextCompressor,
                            AgentProperties properties, PromptResolver promptResolver,
                            PromptService promptService, ProvenanceEmitter provenanceEmitter) {
        this.agentFactory = agentFactory;
        this.contextCompressor = contextCompressor;
        this.properties = properties;
        this.promptResolver = promptResolver;
        this.promptService = promptService;
        this.provenanceEmitter = provenanceEmitter;
    }

    /**
     * Run the streaming agent loop, emitting SSE events as the LLM generates tokens
     * and calls tools.
     *
     * @param model model name (e.g., "coding-agent", "openai/gpt-4o")
     * @param userInput the user's message
     * @param sessionId session identifier (for logging)
     * @param temperature optional temperature override (null = use default)
     * @return Flux of SSE events
     */
    public Flux<ServerSentEvent<String>> streamAgentLoop(
            String model,
            String userInput,
            String sessionId,
            String tenantId,
            Double temperature,
            Integer maxTokens,
            String requestedPromptVersion,
            String requestId,
            String runId
    ) {

        return Flux.create(sink -> {
            // Run the blocking agent loop on boundedElastic to avoid blocking the event loop.
            // Spring AI's ChatModel.call() is synchronous; streaming would require ChatModel.stream()
            // which returns Flux<ChatResponse>. We use stream() when available, falling back to
            // a chunked call() loop.
            Schedulers.boundedElastic().schedule(() -> {
                try {
                    runLoop(
                            sink,
                            model,
                            userInput,
                            sessionId,
                            tenantId,
                            temperature,
                            maxTokens,
                            requestedPromptVersion,
                            requestId,
                            runId
                    );
                } catch (Exception e) {
                    log.error("Agent loop error [session={}]: {}", sessionId, e.getMessage(), e);
                    sink.next(SseEventBuilder.contentChunk(
                            "\n\n❌ Agent error: " + e.getMessage(), model, "stop"));
                    sink.next(SseEventBuilder.done());
                    sink.complete();
                }
            });
        });
    }

    /**
     * The core ReAct loop. Runs on a bounded-elastic thread.
     *
     * <p>Loop structure:
     * <ol>
     *   <li>Compress old tool messages to fit context window</li>
     *   <li>Call LLM with current messages (blocking call for tool detection)</li>
     *   <li>If LLM returns tool calls → execute tools → append results → goto 1</li>
     *   <li>If LLM returns text only → stream it to sink → END</li>
     *   <li>If loop_counter > maxSteps OR consecutive_errors > 3 → force END</li>
     * </ol>
     *
     * <p>WHY blocking call() instead of stream() for tool detection:
     * Ollama (and many providers) don't reliably return tool_calls through the
     * streaming API. Tool calls often only appear in the final aggregated response.
     * We use call() for the ReAct loop where we NEED tool detection, and emit
     * text content as a single chunk once the LLM produces its final answer.
     * This matches how the Python LangGraph agent works internally — tool execution
     * is synchronous between LLM calls; only the FINAL answer is streamed.</p>
     */
    private void runLoop(FluxSink<ServerSentEvent<String>> sink, String model,
                         String userInput, String sessionId, String tenantId,
                         Double temperature, Integer maxTokens,
                         String requestedPromptVersion, String requestId, String runId) {

        int maxSteps = properties.maxAgentSteps();
        int maxToolOutput = properties.maxToolOutput();
        int maxContextChars = properties.maxContextChars();

        String effectiveTenantId = tenantId == null || tenantId.isBlank() ? "default" : tenantId;
        String effectiveRequestId = requestId == null || requestId.isBlank()
                ? "req-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12)
                : requestId;
        String effectiveRunId = runId == null || runId.isBlank()
                ? "run-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12)
                : runId;
        String resolvedModel = resolveModelName(model);
        double effectiveTemperature = temperature != null ? temperature : 0.7;

        PromptResolution promptResolution = promptResolver.resolve(
                effectiveTenantId,
                sessionId,
                requestedPromptVersion
        );
        String systemPrompt = promptService.buildFullPrompt(effectiveTenantId, promptResolution.version());

        // Build the initial prompt
        ChatModel chatModel = agentFactory.getChatModel(model, temperature);
        ToolCallback[] tools = agentFactory.getToolCallbacks();
        ChatOptions promptOptions = agentFactory.buildPromptOptions(model, tools);

        log.info("🚀 Agent loop starting [model={}, tools={}, session={}]",
                model, tools.length, sessionId);

        // State tracking
        List<Message> messages = new ArrayList<>();
        messages.add(new UserMessage(userInput));
        int loopCounter = 0;
        int consecutiveErrors = 0;
        AtomicInteger toolCount = new AtomicInteger(0);
        long startTime = System.currentTimeMillis();

        while (loopCounter < maxSteps) {
            loopCounter++;

            // Step 1: Compress old tool messages to fit context window
            messages = compressMessages(messages, maxContextChars);

            // Step 2: Build the prompt with system message + conversation
            List<Message> fullMessages = new ArrayList<>();
            fullMessages.add(new SystemMessage(systemPrompt));
            fullMessages.addAll(messages);

            Prompt prompt = new Prompt(fullMessages, promptOptions);
            int callPromptTokens = estimateTokens(fullMessages);
            long llmCallStartedMs = System.currentTimeMillis();
            String callSite = "AgentLoopService.runLoop.llm_call_" + loopCounter;

            log.info("🧠 LLM call [loop={}, msgs={}, chars={}, errors={}, session={}]",
                    loopCounter, fullMessages.size(), totalChars(fullMessages),
                    consecutiveErrors, sessionId);

            // Step 3: Call LLM (blocking — needed for reliable tool_call detection)
            ChatResponse response;
            try {
                response = chatModel.call(prompt);
            } catch (Exception e) {
                int durationMs = (int) (System.currentTimeMillis() - llmCallStartedMs);
                emitModelCallProvenance(
                        effectiveRunId,
                        effectiveRequestId,
                        effectiveTenantId,
                        sessionId,
                        callSite,
                        inferProvider(resolvedModel),
                        resolvedModel,
                        effectiveTemperature,
                        maxTokens,
                        promptResolution,
                        callPromptTokens,
                        0,
                        durationMs,
                        consecutiveErrors,
                        "error",
                        e.getClass().getSimpleName()
                );
                log.error("LLM call error [loop={}, session={}]: {}",
                        loopCounter, sessionId, e.getMessage());
                sink.next(SseEventBuilder.contentChunk(
                        "\n\n❌ LLM error: " + e.getMessage(), model, "stop"));
                sink.next(SseEventBuilder.done());
                sink.complete();
                return;
            }

            if (response == null || response.getResults() == null || response.getResults().isEmpty()) {
                int durationMs = (int) (System.currentTimeMillis() - llmCallStartedMs);
                emitModelCallProvenance(
                        effectiveRunId,
                        effectiveRequestId,
                        effectiveTenantId,
                        sessionId,
                        callSite,
                        inferProvider(resolvedModel),
                        resolvedModel,
                        effectiveTemperature,
                        maxTokens,
                        promptResolution,
                        callPromptTokens,
                        0,
                        durationMs,
                        consecutiveErrors,
                        "error",
                        "EmptyResponse"
                );
                log.warn("🛑 Empty response from LLM [session={}]", sessionId);
                break;
            }

            Generation generation = response.getResults().get(0);
            if (generation == null || generation.getOutput() == null) {
                int durationMs = (int) (System.currentTimeMillis() - llmCallStartedMs);
                emitModelCallProvenance(
                        effectiveRunId,
                        effectiveRequestId,
                        effectiveTenantId,
                        sessionId,
                        callSite,
                        inferProvider(resolvedModel),
                        resolvedModel,
                        effectiveTemperature,
                        maxTokens,
                        promptResolution,
                        callPromptTokens,
                        0,
                        durationMs,
                        consecutiveErrors,
                        "error",
                        "NullGeneration"
                );
                log.warn("🛑 Null generation output [session={}]", sessionId);
                break;
            }

            AssistantMessage output = generation.getOutput();
            String textContent = output.getText() != null ? output.getText() : "";
            List<AssistantMessage.ToolCall> toolCalls = output.getToolCalls() != null
                    ? output.getToolCalls() : List.of();
            int durationMs = (int) (System.currentTimeMillis() - llmCallStartedMs);
            emitModelCallProvenance(
                    effectiveRunId,
                    effectiveRequestId,
                    effectiveTenantId,
                    sessionId,
                    callSite,
                    inferProvider(resolvedModel),
                    resolvedModel,
                    effectiveTemperature,
                    maxTokens,
                    promptResolution,
                    callPromptTokens,
                    estimateTokens(textContent),
                    durationMs,
                    consecutiveErrors,
                    "ok",
                    null
            );

            log.debug("📨 LLM response [loop={}, session={}]: toolCalls={}, textLen={}, hasMetadata={}",
                    loopCounter, sessionId, toolCalls.size(), textContent.length(),
                    output.getMetadata() != null ? output.getMetadata().keySet() : "null");

            // Step 4: Process LLM output
            if (!toolCalls.isEmpty()) {
                // LLM wants to call tools
                log.info("🤖 LLM decided to call {} tool(s): {} [session={}]",
                        toolCalls.size(),
                        toolCalls.stream().map(AssistantMessage.ToolCall::name).toList(),
                        sessionId);

                // Append the assistant message with tool calls
                AssistantMessage assistantMsg = AssistantMessage.builder()
                        .content(textContent)
                        .toolCalls(toolCalls)
                        .build();
                messages.add(assistantMsg);

                // Execute each tool call — PARALLEL when multiple calls are returned
                List<ToolResponseMessage.ToolResponse> toolResponses = new ArrayList<>();
                boolean hasError = false;

                if (toolCalls.size() == 1) {
                    // Single tool call — run inline (avoids thread-pool overhead)
                    AssistantMessage.ToolCall toolCall = toolCalls.get(0);
                    toolCount.incrementAndGet();
                    String toolName = toolCall.name();
                    String toolArgs = toolCall.arguments();

                    sink.next(SseEventBuilder.toolStart(toolName,
                            Map.of("arguments", toolArgs), toolCount.get()));
                    sink.next(SseEventBuilder.contentChunk(
                            "\n🔧 Using " + toolName + "...\n", model, null));

                    String toolResult;
                    try {
                        toolResult = executeTool(toolName, toolArgs, tools);
                    } catch (Exception e) {
                        toolResult = "❌ Tool execution failed: " + e.getMessage();
                        hasError = true;
                    }

                    if (toolResult.length() > maxToolOutput) {
                        int omitted = toolResult.length() - maxToolOutput;
                        toolResult = toolResult.substring(0, maxToolOutput)
                                + "\n\n... (truncated, " + omitted + " chars omitted. "
                                + "Use start_line/end_line for specific ranges)";
                    }
                    if (toolResult.startsWith("❌")) hasError = true;

                    String outputPreview = toolResult.length() > 200
                            ? toolResult.substring(0, 200) : toolResult;
                    sink.next(SseEventBuilder.toolEnd(toolName,
                            Map.of("arguments", toolArgs), outputPreview, toolCount.get()));

                    toolResponses.add(new ToolResponseMessage.ToolResponse(
                            toolCall.id(), toolName, toolResult));
                } else {
                    // Multiple tool calls — execute in parallel for concurrency
                    // (especially beneficial when spawning multiple subagents)
                    log.info("⚡ Executing {} tool calls in parallel [session={}]",
                            toolCalls.size(), sessionId);

                    // Emit all start events first
                    for (AssistantMessage.ToolCall toolCall : toolCalls) {
                        toolCount.incrementAndGet();
                        sink.next(SseEventBuilder.toolStart(toolCall.name(),
                                Map.of("arguments", toolCall.arguments()), toolCount.get()));
                        sink.next(SseEventBuilder.contentChunk(
                                "\n🔧 Using " + toolCall.name() + "...\n", model, null));
                    }

                    // Fan out all tool calls concurrently
                    record ToolExecResult(String id, String name, String result, boolean error) {}
                    List<CompletableFuture<ToolExecResult>> futures = new ArrayList<>();
                    for (AssistantMessage.ToolCall toolCall : toolCalls) {
                        String toolName = toolCall.name();
                        String toolArgs = toolCall.arguments();
                        futures.add(CompletableFuture.supplyAsync(() -> {
                            String result;
                            boolean err = false;
                            try {
                                result = executeTool(toolName, toolArgs, tools);
                            } catch (Exception e) {
                                result = "❌ Tool execution failed: " + e.getMessage();
                                err = true;
                            }
                            if (result.length() > maxToolOutput) {
                                int omitted = result.length() - maxToolOutput;
                                result = result.substring(0, maxToolOutput)
                                        + "\n\n... (truncated, " + omitted + " chars omitted)";
                            }
                            if (result.startsWith("❌")) err = true;
                            return new ToolExecResult(toolCall.id(), toolName, result, err);
                        }, TOOL_EXECUTOR));
                    }

                    // Collect results (preserving order for deterministic conversation)
                    for (CompletableFuture<ToolExecResult> future : futures) {
                        try {
                            ToolExecResult res = future.join();
                            if (res.error()) hasError = true;
                            String outputPreview = res.result().length() > 200
                                    ? res.result().substring(0, 200) : res.result();
                            sink.next(SseEventBuilder.toolEnd(res.name(),
                                    Map.of(), outputPreview, toolCount.get()));
                            toolResponses.add(new ToolResponseMessage.ToolResponse(
                                    res.id(), res.name(), res.result()));
                        } catch (Exception e) {
                            log.error("Tool future failed [session={}]: {}", sessionId, e.getMessage());
                            hasError = true;
                        }
                    }
                }

                // Append tool responses to conversation
                messages.add(ToolResponseMessage.builder().responses(toolResponses).build());

                // Track consecutive errors
                if (hasError) {
                    consecutiveErrors++;
                    log.warn("⚠️ Tool error detected (consecutive: {}/3) [session={}]",
                            consecutiveErrors, sessionId);
                    if (consecutiveErrors <= 3) {
                        String hint = "⚠️ The previous tool call returned an error. "
                                + "Review the error and try a different approach. "
                                + "(attempt " + consecutiveErrors + "/3)";
                        messages.add(new UserMessage(hint));
                    }
                } else {
                    if (consecutiveErrors > 0) {
                        log.debug("✅ Error streak reset (was {}) [session={}]",
                                consecutiveErrors, sessionId);
                    }
                    consecutiveErrors = 0;
                }

                // Guard: too many consecutive errors
                if (consecutiveErrors > 3) {
                    log.warn("🛑 Max consecutive errors reached, forcing end [session={}]", sessionId);
                    break;
                }

                // Loop continues — next iteration will call LLM with tool results

            } else {
                // LLM returned text only (no tool calls) — agent is done
                // Emit the text as SSE content chunk
                if (!textContent.isEmpty()) {
                    sink.next(SseEventBuilder.contentChunk(textContent, model, null));
                    log.info("🤖 LLM final response [loop={}, tools={}, session={}]: {}",
                            loopCounter, toolCount.get(), sessionId,
                            textContent.length() > 120 ? textContent.substring(0, 120) + "..." : textContent);
                }
                break;
            }
        }

        // Emit completion events
        long durationMs = System.currentTimeMillis() - startTime;
        sink.next(SseEventBuilder.status("complete", Map.of(
                "request_id", effectiveRequestId,
                "run_id", effectiveRunId,
                "tool_count", toolCount.get(),
                "duration_ms", durationMs
        )));
        sink.next(SseEventBuilder.contentChunk(null, model, "stop"));
        sink.next(SseEventBuilder.done());
        sink.complete();
    }

    /**
     * Execute a tool by name, dispatching to the matching ToolCallback.
     */
    private String executeTool(String toolName, String arguments, ToolCallback[] tools) {
        for (ToolCallback tool : tools) {
            if (tool.getToolDefinition().name().equals(toolName)) {
                return tool.call(arguments);
            }
        }
        return "❌ Unknown tool: " + toolName;
    }

    private void emitModelCallProvenance(
            String runId,
            String requestId,
            String tenantId,
            String sessionId,
            String callSite,
            String provider,
            String model,
            double temperature,
            Integer maxTokens,
            PromptResolution promptResolution,
            int promptTokens,
            int completionTokens,
            int durationMs,
            int retryCount,
            String status,
            String errorClass
    ) {
        ModelCallProvenance event = new ModelCallProvenance(
                "mcall-" + UUID.randomUUID().toString().replace("-", ""),
                runId,
                requestId,
                null,
                tenantId,
                sessionId,
                "java-loop",
                callSite,
                provider,
                model,
                temperature,
                normalizeMaxTokens(maxTokens),
                promptResolution.promptId(),
                promptResolution.version(),
                promptResolution.contentHash(),
                buildFeatureFlagsSnapshot(promptResolution),
                Math.max(0, promptTokens),
                Math.max(0, completionTokens),
                Math.max(0, durationMs),
                0,
                Math.max(0, retryCount),
                status,
                errorClass,
                Instant.now()
        );
        try {
            provenanceEmitter.emitModelCall(event);
        } catch (Exception emitError) {
            log.debug("Failed to emit model-call provenance [session={}]: {}",
                    sessionId, emitError.getMessage());
        }
    }

    private Integer normalizeMaxTokens(Integer maxTokens) {
        if (maxTokens == null || maxTokens <= 0) {
            return null;
        }
        return maxTokens;
    }

    private Map<String, Object> buildFeatureFlagsSnapshot(PromptResolution promptResolution) {
        Map<String, Object> flags = new LinkedHashMap<>();
        flags.put("prompt_version_default", properties.promptVersion());
        flags.put("prompt_assignment_source", promptResolution.assignmentSource());
        flags.put("max_context_chars", properties.maxContextChars());
        flags.put("max_agent_steps", properties.maxAgentSteps());
        return flags;
    }

    private int estimateTokens(List<Message> messages) {
        int totalChars = 0;
        for (Message message : messages) {
            String text = message.getText();
            if (text != null) {
                totalChars += text.length();
            }
        }
        return totalChars <= 0 ? 0 : Math.max(1, totalChars / 4);
    }

    private int estimateTokens(String text) {
        if (text == null || text.isBlank()) {
            return 0;
        }
        return Math.max(1, text.length() / 4);
    }

    private String resolveModelName(String modelName) {
        if (modelName == null || modelName.isBlank() || "coding-agent".equals(modelName)) {
            return properties.defaultModel();
        }
        return modelName;
    }

    private String inferProvider(String modelName) {
        if (modelName.startsWith("openai/") || modelName.startsWith("gpt-")) {
            return "openai";
        }
        if (modelName.startsWith("anthropic/") || modelName.startsWith("claude")) {
            return "anthropic";
        }
        return "ollama";
    }

    /**
     * Compress old tool messages to fit within the context window budget.
     *
     * <p>Strategy (mirrors Python graph_v2.py):
     * <ol>
     *   <li>Keep the 4 most recent tool responses uncompressed</li>
     *   <li>Compress older tool responses using smart (tool-type-aware) compression</li>
     *   <li>If still over budget, drop oldest messages</li>
     * </ol>
     */
    private List<Message> compressMessages(List<Message> messages, int maxChars) {
        // Find tool response messages
        List<Integer> toolIndices = new ArrayList<>();
        for (int i = 0; i < messages.size(); i++) {
            if (messages.get(i) instanceof ToolResponseMessage) {
                toolIndices.add(i);
            }
        }

        // Only compress if we have more than 4 tool responses
        if (toolIndices.size() > 4) {
            List<Integer> oldIndices = toolIndices.subList(0, toolIndices.size() - 4);
            List<Message> result = new ArrayList<>(messages);

            for (int idx : oldIndices) {
                Message msg = result.get(idx);
                if (msg instanceof ToolResponseMessage toolMsg) {
                    List<ToolResponseMessage.ToolResponse> compressed = new ArrayList<>();
                    for (ToolResponseMessage.ToolResponse resp : toolMsg.getResponses()) {
                        String content = resp.responseData();
                        if (content != null && content.length() > 1500) {
                            String compressedContent = contextCompressor.smartCompress(
                                    content, 1500, resp.name());
                            compressed.add(new ToolResponseMessage.ToolResponse(
                                    resp.id(), resp.name(), compressedContent));
                            log.debug("📦 Compressed tool output [{}]: {} → {} chars",
                                    resp.name(), content.length(), compressedContent.length());
                        } else {
                            compressed.add(resp);
                        }
                    }
                    result.set(idx, ToolResponseMessage.builder().responses(compressed).build());
                }
            }
            messages = result;
        }

        // Enforce character budget — drop oldest messages if over budget
        int total = totalChars(messages);
        if (total > maxChars) {
            List<Message> kept = new ArrayList<>();
            int running = 0;
            for (int i = messages.size() - 1; i >= 0; i--) {
                Message msg = messages.get(i);
                int msgLen = msg.getText() != null ? msg.getText().length() : 0;
                if (running + msgLen > maxChars) {
                    break;
                }
                kept.add(0, msg);
                running += msgLen;
            }
            int dropped = messages.size() - kept.size();
            if (dropped > 0) {
                log.info("📦 Dropped {} oldest messages to fit context budget ({} chars)",
                        dropped, maxChars);
            }
            messages = kept;
        }

        return messages;
    }

    private int totalChars(List<Message> messages) {
        int total = 0;
        for (Message msg : messages) {
            String text = msg.getText();
            if (text != null) {
                total += text.length();
            }
        }
        return total;
    }
}
















