package com.example.agent.api;

import com.example.agent.agent.AgentService;
import com.example.agent.agent.FleetBus;
import com.example.agent.agent.IntentClassifier;
import com.example.agent.config.AgentProperties;
import com.example.agent.session.ConversationStore;
import com.example.agent.session.Message;
import com.example.agent.streaming.SseEventBuilder;
import com.example.mcp.common.security.TenantContext;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@RestController
public class ChatController {

    private static final Logger log = LoggerFactory.getLogger(ChatController.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final AgentService agentService;
    private final IntentClassifier intentClassifier;
    private final ConversationStore conversationStore;
    private final AgentProperties properties;
    private final FleetBus fleetBus;
    private final com.example.agent.agent.UsageTracker usageTracker;

    public ChatController(AgentService agentService, IntentClassifier intentClassifier,
                          ConversationStore conversationStore, AgentProperties properties,
                          FleetBus fleetBus, com.example.agent.agent.UsageTracker usageTracker) {
        this.agentService = agentService;
        this.intentClassifier = intentClassifier;
        this.conversationStore = conversationStore;
        this.properties = properties;
        this.fleetBus = fleetBus;
        this.usageTracker = usageTracker;
    }

    @PostMapping("/v1/chat/completions")
    public Mono<ResponseEntity<?>> chatCompletions(@RequestBody ChatRequest request) {
        if (request.messages() == null || request.messages().isEmpty()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "messages is required");
        }

        return prepareRequest(request).flatMap(prepared -> {
            if (Boolean.TRUE.equals(request.stream())) {
                return Mono.just(ResponseEntity.ok()
                        .contentType(MediaType.TEXT_EVENT_STREAM)
                        .body(streamingResponse(prepared)));
            }
            return nonStreamingResponse(prepared).map(ResponseEntity::ok);
        });
    }

    private Flux<ServerSentEvent<String>> streamingResponse(PreparedChat prepared) {
        if (prepared.metaAnswer() != null) {
            return conversationStore.append(prepared.sessionId(), new Message("user", prepared.userInput()))
                    .then(conversationStore.append(prepared.sessionId(), new Message("assistant", prepared.metaAnswer())))
                    .thenMany(Flux.just(
                            SseEventBuilder.contentChunk(prepared.metaAnswer(), prepared.request().model(), null),
                            SseEventBuilder.contentChunk(null, prepared.request().model(), "stop"),
                            SseEventBuilder.done()
                    ));
        }
        return agentService.streamResponse(
                prepared.request(),
                prepared.sessionId(),
                prepared.tenantId(),
                prepared.requestId(),
                prepared.runId()
        );
    }

    private Mono<Map<String, Object>> nonStreamingResponse(PreparedChat prepared) {
        if (prepared.metaAnswer() != null) {
            return conversationStore.append(prepared.sessionId(), new Message("user", prepared.userInput()))
                    .then(conversationStore.append(prepared.sessionId(), new Message("assistant", prepared.metaAnswer())))
                    .thenReturn(completionResponse(prepared.request().model(), prepared.metaAnswer()));
        }

        return agentService.streamResponse(
                        prepared.request(),
                        prepared.sessionId(),
                        prepared.tenantId(),
                        prepared.requestId(),
                        prepared.runId()
                )
                .filter(event -> event.event() == null || event.event().isEmpty())
                .mapNotNull(ServerSentEvent::data)
                .takeUntil("[DONE]"::equals)
                .filter(data -> !"[DONE]".equals(data))
                .flatMap(this::extractContentChunk)
                .collectList()
                .map(chunks -> completionResponse(prepared.request().model(), String.join("", chunks)));
    }

    private Mono<PreparedChat> prepareRequest(ChatRequest request) {
        String tenantId = TenantContext.getOrNull();
        if (tenantId == null || tenantId.isBlank()) {
            tenantId = "default";
        }

        String sessionId = request.sessionId() != null && !request.sessionId().isBlank()
                ? request.sessionId()
                : "session-" + UUID.randomUUID().toString().substring(0, 12);
        sessionId = tenantId + ":" + sessionId;
        String requestId = "req-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        String runId = "run-" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);

        String userInput = request.lastUserMessage();
        ChatRequest updatedRequest = request;
        if (request.activeFile() != null) {
            ChatRequest.ActiveFileContext activeFile = request.activeFile();
            StringBuilder hint = new StringBuilder("[Active file: ").append(activeFile.path());
            if (activeFile.visibleStart() != null) {
                hint.append(", lines ").append(activeFile.visibleStart()).append("-").append(activeFile.visibleEnd());
            }
            hint.append("]\n");
            userInput = hint + userInput;
            updatedRequest = request.withLastUserMessage(userInput);
        }

        String metaAnswer = null;
        if (intentClassifier.isMetaQuestion(userInput)) {
            String answer = intentClassifier.getMetaAnswer(userInput);
            if (answer != null && !answer.isEmpty()) {
                metaAnswer = answer;
            }
        }

        ChatRequest finalUpdatedRequest = updatedRequest;
        String finalUserInput = userInput;
        String finalTenantId = tenantId;
        String finalRequestId = requestId;
        String finalRunId = runId;
        String initialSessionId = sessionId;
        String finalMetaAnswer = metaAnswer;

        return conversationStore.getMessages(initialSessionId)
                .collectList()
                .map(history -> {
                    String resolvedSessionId = initialSessionId;
                    if (finalMetaAnswer == null && intentClassifier.detectTopicSwitch(finalUserInput, history)) {
                        resolvedSessionId = initialSessionId + "-" + UUID.randomUUID().toString().substring(0, 6);
                    }
                    return new PreparedChat(
                            finalUpdatedRequest,
                            resolvedSessionId,
                            finalTenantId,
                            finalRequestId,
                            finalRunId,
                            finalUserInput,
                            finalMetaAnswer
                    );
                });
    }

    private Mono<String> extractContentChunk(String data) {
        try {
            JsonNode json = MAPPER.readTree(data);
            JsonNode content = json.path("choices").path(0).path("delta").path("content");
            return Mono.just(content.isMissingNode() || content.isNull() ? "" : content.asText(""));
        } catch (Exception e) {
            log.debug("Skipping non-content SSE chunk", e);
            return Mono.empty();
        }
    }

    private Map<String, Object> completionResponse(String model, String content) {
        LinkedHashMap<String, Object> response = new LinkedHashMap<>();
        response.put("id", "chatcmpl-" + UUID.randomUUID().toString().replace("-", "").substring(0, 8));
        response.put("object", "chat.completion");
        response.put("created", Instant.now().getEpochSecond());
        response.put("model", model);
        response.put("choices", List.of(Map.of(
                "index", 0,
                "message", Map.of("role", "assistant", "content", content),
                "finish_reason", "stop"
        )));
        response.put("usage", Map.of("prompt_tokens", 0, "completion_tokens", 0, "total_tokens", 0));
        return response;
    }

    @GetMapping("/v1/models")
    public Map<String, Object> listModels() {
        return Map.of(
                "object", "list",
                "data", List.of(Map.of(
                        "id", "coding-agent",
                        "object", "model",
                        "owned_by", "local"
                ))
        );
    }

    @GetMapping("/api/usage")
    public Map<String, Object> usage() {
        return usageTracker.getSummary();
    }

    @GetMapping("/health")
    public Map<String, Object> health() {
        return Map.of(
                "status", "ok",
                "model", properties.defaultModel()
        );
    }

    /**
     * Request cooperative cancellation of an in-flight subagent.
     *
     * <p>Cancellation is cooperative: the child checks the flag between tool rounds.
     * Returns 202 even when the session/child is unknown — that's a race, not an error.</p>
     */
    @PostMapping("/v1/sessions/{sessionId}/children/{childId}/cancel")
    public Map<String, Object> cancelChild(@PathVariable String sessionId, @PathVariable String childId) {
        String tenantId = TenantContext.getOrNull();
        if (tenantId == null) tenantId = "default";

        // Tenant isolation — session_id format is "{tenant}:{user_session}"
        String expectedPrefix = tenantId + ":";
        if (!sessionId.startsWith(expectedPrefix)) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "session does not belong to this tenant");
        }

        boolean accepted = fleetBus.requestCancel(sessionId, childId);
        return Map.of(
                "session_id", sessionId,
                "child_session_id", childId,
                "accepted", accepted,
                "note", accepted
                        ? "cooperative cancel — honored between tool rounds"
                        : "session not found (child may have already completed)"
        );
    }

    /**
     * Subscribe to fleet bus events for a session (SSE stream for dashboards).
     */
    @GetMapping(value = "/v1/sessions/{sessionId}/events", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<ServerSentEvent<String>> sessionEvents(@PathVariable String sessionId) {
        return fleetBus.subscribe(sessionId)
                .map(event -> {
                    try {
                        String data = MAPPER.writeValueAsString(event);
                        String type = (String) event.getOrDefault("type", "fleet_event");
                        return ServerSentEvent.<String>builder()
                                .event(type)
                                .data(data)
                                .build();
                    } catch (Exception e) {
                        return ServerSentEvent.<String>builder()
                                .event("error")
                                .data("{\"error\":\"serialization failed\"}")
                                .build();
                    }
                });
    }

    private record PreparedChat(ChatRequest request, String sessionId, String tenantId,
                                String requestId, String runId, String userInput, String metaAnswer) {
    }
}
