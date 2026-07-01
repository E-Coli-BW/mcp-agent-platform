package com.example.agent.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Date;
import java.util.Map;

/**
 * REST client for calling MCP tool backends (memory-server, codeexec-server, etc.).
 *
 * <p>Authentication strategy (mirrors Python McpToolClient):
 * <ol>
 *   <li>AuthServiceClient (centralized RS256 JWT from auth service) — preferred</li>
 *   <li>Self-signed HMAC JWT (legacy fallback if auth service unavailable)</li>
 * </ol>
 */
public class McpRestClient {

    private static final Logger log = LoggerFactory.getLogger(McpRestClient.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final WebClient webClient;
    private final String jwtSecret;
    private final Duration timeout;
    private final AuthServiceClient authClient;
    private final String audience;

    public McpRestClient(String baseUrl, String jwtSecret, Duration timeout) {
        this(baseUrl, jwtSecret, timeout, null, "mcp-platform");
    }

    public McpRestClient(String baseUrl, String jwtSecret, Duration timeout,
                         AuthServiceClient authClient, String audience) {
        this.webClient = WebClient.builder()
            .baseUrl(baseUrl)
            .build();
        this.jwtSecret = jwtSecret;
        this.timeout = timeout;
        this.authClient = authClient;
        this.audience = audience;
    }

    public Mono<String> callTool(String toolName, Map<String, Object> args, String tenantId) {
        String token = resolveToken(tenantId);
        return webClient.post()
            .uri("/api/tools/{name}", toolName)
            .header("Authorization", "Bearer " + token)
            .bodyValue(args != null ? args : Map.of())
            .retrieve()
            .bodyToMono(String.class)
            .map(this::extractResult)
            .timeout(timeout)
            .onErrorResume(ex -> {
                String msg = ex.getMessage();
                // On 401, invalidate auth cache and hint at the issue
                if (msg != null && msg.contains("401")) {
                    if (authClient != null) {
                        authClient.invalidate(audience);
                    }
                }
                log.warn("MCP call failed: {} → {}", toolName, msg);
                return Mono.just("❌ Service unavailable: " + toolName);
            });
    }

    /**
     * Resolve token: try auth service (RS256) first, fall back to HMAC.
     */
    private String resolveToken(String tenantId) {
        // Strategy 1: Auth service (RS256)
        if (authClient != null) {
            String token = authClient.getToken(audience, tenantId);
            if (token != null) {
                return token;
            }
        }

        // Strategy 2: Legacy self-signed HMAC
        return generateHmacToken(tenantId);
    }

    private String generateHmacToken(String tenantId) {
        SecretKey key = Keys.hmacShaKeyFor(jwtSecret.getBytes(StandardCharsets.UTF_8));
        return Jwts.builder()
            .subject("agent-server")
            .claim("tenant_id", tenantId != null ? tenantId : "default")
            .issuedAt(Date.from(Instant.now()))
            .expiration(Date.from(Instant.now().plusSeconds(300)))
            .signWith(key)
            .compact();
    }

    private String extractResult(String responseBody) {
        try {
            JsonNode node = MAPPER.readTree(responseBody);
            JsonNode result = node.get("result");
            if (result == null || result.isNull()) {
                return responseBody;
            }
            return result.isTextual() ? result.asText() : MAPPER.writeValueAsString(result);
        } catch (Exception ignored) {
            return responseBody;
        }
    }
}
