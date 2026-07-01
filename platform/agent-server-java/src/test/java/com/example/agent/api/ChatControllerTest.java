package com.example.agent.api;

import com.example.agent.agent.AgentFactory;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.ai.chat.messages.AssistantMessage;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.ai.chat.model.Generation;
import org.springframework.ai.chat.prompt.ChatOptions;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.test.autoconfigure.web.reactive.AutoConfigureWebTestClient;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.reactive.server.FluxExchangeResult;
import org.springframework.test.web.reactive.server.WebTestClient;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.Duration;
import java.util.Date;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureWebTestClient
class ChatControllerTest {

    @Autowired
    private WebTestClient webTestClient;

    @Value("${agent.jwt-secret}")
    private String jwtSecret;

    /**
     * Mock AgentFactory so the controller never calls a real LLM during tests.
     * Without this, every assertion-positive test in this class POSTs to
     * /v1/chat/completions which triggers AgentLoopService.runLoop →
     * ChatModel.call(prompt) → real HTTP to Ollama at localhost:11434.
     * On any dev machine without the configured model installed, that call
     * returns HTTP 404 and the catch block in runLoop logs an ERROR plus
     * a SSE error chunk. The test still passes (the error path correctly
     * emits [DONE]) but the test output is polluted with stack traces and
     * red ERROR lines, which is noise that masks real failures in CI logs.
     */
    @MockitoBean
    private AgentFactory agentFactory;

    @BeforeEach
    void stubAgentFactory() {
        ChatModel fakeModel = Mockito.mock(ChatModel.class);
        AssistantMessage assistant = new AssistantMessage("ok from test");
        ChatResponse response = new ChatResponse(List.of(new Generation(assistant)));
        Mockito.when(fakeModel.call(Mockito.any(Prompt.class))).thenReturn(response);

        ChatOptions stubOptions = ChatOptions.builder().build();

        Mockito.when(agentFactory.getChatModel(Mockito.anyString(), Mockito.any()))
                .thenReturn(fakeModel);
        Mockito.when(agentFactory.getToolCallbacks()).thenReturn(new ToolCallback[0]);
        Mockito.when(agentFactory.buildPromptOptions(Mockito.anyString(), Mockito.any(ToolCallback[].class)))
                .thenReturn(stubOptions);
    }

    private String createTestToken() {
        byte[] keyBytes = jwtSecret.getBytes(StandardCharsets.UTF_8);
        if (keyBytes.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
            keyBytes = padded;
        }
        return Jwts.builder()
                .subject("test-agent")
                .claim("tenant_id", "test-tenant")
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plusSeconds(3600)))
                .signWith(Keys.hmacShaKeyFor(keyBytes))
                .compact();
    }

    @Test
    void shouldReturnOpenAiCompletionWhenStreamDisabled() {
        webTestClient.post()
                .uri("/v1/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .header("Authorization", "Bearer " + createTestToken())
                .bodyValue("""
                        {
                          "model": "coding-agent",
                          "stream": false,
                          "messages": [
                            {"role": "user", "content": "hello from test"}
                          ]
                        }
                        """)
                .exchange()
                .expectStatus().isOk()
                .expectBody()
                .jsonPath("$.object").isEqualTo("chat.completion")
                .jsonPath("$.choices[0].message.content").exists();
    }

    @Test
    void shouldStreamSseWhenStreamEnabled() {
        FluxExchangeResult<String> result = webTestClient.post()
                .uri("/v1/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .header("Authorization", "Bearer " + createTestToken())
                .bodyValue("""
                        {
                          "model": "coding-agent",
                          "stream": true,
                          "messages": [
                            {"role": "user", "content": "stream please"}
                          ]
                        }
                        """)
                .exchange()
                .expectStatus().isOk()
                .expectHeader().contentTypeCompatibleWith(MediaType.TEXT_EVENT_STREAM)
                .returnResult(String.class);

        List<String> body = result.getResponseBody()
                .takeUntil(s -> s != null && s.contains("[DONE]"))
                .collectList()
                .block(Duration.ofSeconds(60));
        assertThat(body).isNotNull().isNotEmpty();
        String joined = String.join("\n", body);
        assertThat(joined).contains("[DONE]");
        assertThat(joined).contains("\"request_id\":\"req-");
        assertThat(joined).contains("\"run_id\":\"run-");
    }

    @Test
    void shouldReturn401WhenNoToken() {
        webTestClient.post()
                .uri("/v1/chat/completions")
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue("""
                        {
                          "model": "coding-agent",
                          "stream": false,
                          "messages": [{"role": "user", "content": "no auth"}]
                        }
                        """)
                .exchange()
                .expectStatus().isUnauthorized();
    }
}
