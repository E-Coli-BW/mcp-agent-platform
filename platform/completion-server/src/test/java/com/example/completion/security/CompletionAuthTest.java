package com.example.completion.security;

import com.example.completion.proxy.OllamaStreamingProxy;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.reactive.AutoConfigureWebTestClient;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.reactive.server.WebTestClient;
import reactor.core.publisher.Flux;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

@SpringBootTest(
        webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = {
                "mcp.security.jwt-secret=test-secret-at-least-32-bytes-long!!",
                "mcp.security.jwks-url=",
                "completion.ollama.timeout-ms=100"
        })
@AutoConfigureWebTestClient
class CompletionAuthTest {

    private static final String JWT_SECRET = "test-secret-at-least-32-bytes-long!!";

    @Autowired
    private WebTestClient webTestClient;

    @MockBean
    private OllamaStreamingProxy proxy;

    @BeforeEach
    void setUp() {
        when(proxy.generate(any(), any(), any())).thenReturn(Flux.just("completion"));
    }

    @Test
    void should_reject_when_noAuthorizationHeader() {
        webTestClient.post()
                .uri("/v1/completions")
                .accept(MediaType.APPLICATION_JSON)
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue("""
                        {
                          "file_content": "class Test {}",
                          "cursor_line": 0,
                          "cursor_column": 0,
                          "stream": false
                        }
                        """)
                .exchange()
                .expectStatus().isUnauthorized();
    }

    @Test
    void should_reject_when_invalidJwt() {
        webTestClient.post()
                .uri("/v1/completions")
                .header("Authorization", "Bearer invalid-token")
                .accept(MediaType.APPLICATION_JSON)
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue("""
                        {
                          "file_content": "class Test {}",
                          "cursor_line": 0,
                          "cursor_column": 0,
                          "stream": false
                        }
                        """)
                .exchange()
                .expectStatus().isUnauthorized();
    }

    @Test
    void should_accept_when_validJwt() {
        webTestClient.post()
                .uri("/v1/completions")
                .header("Authorization", "Bearer " + validJwt())
                .accept(MediaType.APPLICATION_JSON)
                .contentType(MediaType.APPLICATION_JSON)
                .bodyValue("""
                        {
                          "file_content": "class Test {}",
                          "cursor_line": 0,
                          "cursor_column": 0,
                          "stream": false
                        }
                        """)
                .exchange()
                .expectStatus().isOk()
                .expectBody()
                .jsonPath("$.choices[0].text").isEqualTo("completion");
    }

    @Test
    void should_returnOk_when_requestingActuatorHealthWithoutJwt() {
        webTestClient.get()
                .uri("/actuator/health")
                .exchange()
                .expectStatus().isOk();
    }

    private String validJwt() {
        return Jwts.builder()
                .subject("test-service")
                .claim("tenant_id", "completion-test-tenant")
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plus(1, ChronoUnit.HOURS)))
                .signWith(Keys.hmacShaKeyFor(JWT_SECRET.getBytes(StandardCharsets.UTF_8)))
                .compact();
    }
}
