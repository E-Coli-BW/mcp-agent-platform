package com.example.memoryserver.integration;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.test.context.ActiveProfiles;

import javax.crypto.SecretKey;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Raw HTTP auth tests — uses java.net.http.HttpClient (NOT TestRestTemplate)
 * to avoid Spring's auto-injected Basic auth credentials.
 *
 * This is the DEFINITIVE test for "does /api/** actually reject unauthenticated requests?"
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
        properties = "mcp.security.jwt-secret=test-secret-at-least-32-bytes-long!!")
@ActiveProfiles("integration-test")
class RawHttpAuthTest {

    @LocalServerPort
    private int port;

    private final HttpClient httpClient = HttpClient.newHttpClient();
    private static final String JWT_SECRET = "test-secret-at-least-32-bytes-long!!";

    private String baseUrl() {
        return "http://localhost:" + port;
    }

    private String generateValidJwt(String tenantId) {
        byte[] keyBytes = JWT_SECRET.getBytes(StandardCharsets.UTF_8);
        if (keyBytes.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
            keyBytes = padded;
        }
        SecretKey key = Keys.hmacShaKeyFor(keyBytes);
        return Jwts.builder()
                .subject("test-service")
                .claim("tenant_id", tenantId)
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plus(1, ChronoUnit.HOURS)))
                .signWith(key)
                .compact();
    }

    @Test
    void noJwt_noBasicAuth_shouldBeRejected() throws Exception {
        var request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_context"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{}"))
                .build();

        var response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        assertNotEquals(200, response.statusCode(),
                "SECURITY BUG: /api/** returned 200 WITHOUT any authentication! " +
                "Response: " + response.body());
        assertTrue(response.statusCode() == 401 || response.statusCode() == 403,
                "Expected 401 or 403, got: " + response.statusCode());
    }

    @Test
    void invalidJwt_shouldBeRejected() throws Exception {
        var request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_context"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer garbage.invalid.token")
                .POST(HttpRequest.BodyPublishers.ofString("{}"))
                .build();

        var response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        assertNotEquals(200, response.statusCode(),
                "SECURITY BUG: /api/** returned 200 with INVALID JWT! " +
                "Response: " + response.body());
    }

    @Test
    void validJwt_shouldSucceed() throws Exception {
        var request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/api/tools/memory_context"))
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + generateValidJwt("test-tenant"))
                .POST(HttpRequest.BodyPublishers.ofString("{}"))
                .build();

        var response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        assertEquals(200, response.statusCode(),
                "Valid JWT should be accepted. Response: " + response.body());
    }

    @Test
    void healthEndpoint_noAuth_shouldSucceed() throws Exception {
        var request = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl() + "/actuator/health"))
                .GET()
                .build();

        var response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

        // Health may return 200 or 503 (degraded) but never 401/403
        assertTrue(response.statusCode() == 200 || response.statusCode() == 503,
                "Health endpoint should not require auth. Got: " + response.statusCode());
    }
}
