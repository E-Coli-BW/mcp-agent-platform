package com.example.agent.agent;

import com.example.agent.api.ChatRequest;
import com.example.agent.api.ChatRequest.ChatMessage;
import com.example.agent.session.ConversationStore;
import com.example.agent.session.Message;
import com.example.agent.session.SessionLane;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class AgentServiceStatusEventTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void shouldIncludeRequestAndRunIdsInThinkingStatus_whenStreamingStarts() throws Exception {
        AgentLoopService loopService = mock(AgentLoopService.class);
        ConversationStore conversationStore = mock(ConversationStore.class);
        SessionLane sessionLane = mock(SessionLane.class);

        when(conversationStore.append(anyString(), any(Message.class))).thenReturn(Mono.empty());
        when(sessionLane.waitForLock(anyString(), any(Duration.class))).thenReturn(Mono.just(true));
        when(sessionLane.releaseLock(anyString())).thenReturn(Mono.empty());
        when(loopService.streamAgentLoop(
                anyString(), anyString(), anyString(), anyString(),
                any(), any(), any(), anyString(), anyString()
        )).thenReturn(Flux.just(ServerSentEvent.<String>builder().data("[DONE]").build()));

        AgentService agentService = new AgentService(loopService, conversationStore, sessionLane);
        ChatRequest request = new ChatRequest(
                "coding-agent",
                List.of(new ChatMessage("user", "hello")),
                true,
                null,
                null,
                null,
                "session-1",
                null
        );

        ServerSentEvent<String> firstEvent = agentService.streamResponse(
                request,
                "tenant-a:session-1",
                "tenant-a",
                "req-123",
                "run-456"
        ).blockFirst();

        assertThat(firstEvent).isNotNull();
        assertThat(firstEvent.event()).isEqualTo("status");

        JsonNode status = MAPPER.readTree(firstEvent.data());
        assertThat(status.path("state").asText()).isEqualTo("thinking");
        assertThat(status.path("request_id").asText()).isEqualTo("req-123");
        assertThat(status.path("run_id").asText()).isEqualTo("run-456");
    }
}

