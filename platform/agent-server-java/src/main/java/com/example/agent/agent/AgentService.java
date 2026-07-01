package com.example.agent.agent;

import com.example.agent.api.ChatRequest;
import com.example.agent.session.ConversationStore;
import com.example.agent.session.Message;
import com.example.agent.session.SessionLane;
import com.example.agent.streaming.SseEventBuilder;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Service;
import reactor.core.publisher.Flux;

import java.time.Duration;
import java.util.Map;

/**
 * Core agent orchestration — manages session lifecycle and delegates to the
 * streaming ReAct agent loop.
 *
 * <p>Responsibilities:
 * <ul>
 *   <li>Save user message to conversation store</li>
 *   <li>Acquire session lock (LANE) to prevent interleaved requests</li>
 *   <li>Delegate to {@link AgentLoopService} for the actual LLM streaming loop</li>
 *   <li>Save assistant response when complete</li>
 *   <li>Release session lock in finally</li>
 * </ul>
 *
 * <p>The streaming agent loop is driven by {@link AgentLoopService}, which implements
 * the ReAct pattern: call LLM → stream tokens → detect tool calls → execute → loop.
 * This replaces the previous blocking {@code ChatClient.prompt().call()} approach.</p>
 */
@Service
public class AgentService {

    private static final Logger log = LoggerFactory.getLogger(AgentService.class);

    private final AgentLoopService agentLoopService;
    private final ConversationStore conversationStore;
    private final SessionLane sessionLane;

    public AgentService(AgentLoopService agentLoopService, ConversationStore conversationStore,
                        SessionLane sessionLane) {
        this.agentLoopService = agentLoopService;
        this.conversationStore = conversationStore;
        this.sessionLane = sessionLane;
    }

    /**
     * Stream an agent response as SSE events.
     *
     * <p>Steps: save user message → emit "thinking" → acquire session lock →
     * run streaming agent loop → emit "complete" → release lock.</p>
     */
    public Flux<ServerSentEvent<String>> streamResponse(
            ChatRequest request,
            String sessionId,
            String tenantId,
            String requestId,
            String runId
    ) {
        String model = request.model();
        String userInput = request.lastUserMessage();

        log.info("📩 User message received [session={}, model={}]: {}", sessionId, model,
                userInput.length() > 120 ? userInput.substring(0, 120) + "..." : userInput);

        return Flux.concat(
                // Save user message
                conversationStore.append(sessionId, new Message("user", userInput)).thenMany(Flux.empty()),

                // Emit "thinking" status
                Flux.just(SseEventBuilder.status("thinking", Map.of(
                        "request_id", requestId,
                        "run_id", runId
                ))),

                // Acquire session lock, then stream
                sessionLane.waitForLock(sessionId, Duration.ofSeconds(30))
                        .flatMapMany(acquired -> {
                            if (!acquired) {
                                return Flux.just(
                                        SseEventBuilder.contentChunk(
                                                "⚠️ Another request for this session is still processing.",
                                                model, "stop"),
                                        SseEventBuilder.done()
                                );
                            }
                            // Delegate to the streaming agent loop
                            return agentLoopService.streamAgentLoop(
                                            model,
                                            userInput,
                                            sessionId,
                                            tenantId,
                                            request.temperature(),
                                            request.maxTokens(),
                                            request.promptVersion(),
                                            requestId,
                                            runId
                                    )
                                    .doOnNext(event -> {
                                        // Capture assistant response for conversation store
                                        // (handled inside the loop for the final content)
                                    })
                                    .doFinally(signal -> sessionLane.releaseLock(sessionId).subscribe());
                        })
        ).onErrorResume(e -> {
            log.error("Agent error for session {}: {}", sessionId, e.getMessage(), e);
            return Flux.just(
                    SseEventBuilder.contentChunk("\n\n❌ Agent error: " + e.getMessage(), model, "stop"),
                    SseEventBuilder.done()
            );
        });
    }
}
