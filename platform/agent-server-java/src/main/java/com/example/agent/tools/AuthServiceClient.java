package com.example.agent.tools;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Auth Service client — requests RS256 JWT tokens from the centralized auth service.
 *
 * <p>Mirrors the Python {@code AuthServiceClient} behavior:
 * <ul>
 *   <li>Calls POST /auth/token with client_credentials grant</li>
 *   <li>Caches tokens per audience+tenant with 60s refresh margin</li>
 *   <li>Gracefully degrades: returns null if auth service is unavailable</li>
 *   <li>Retries after 30s cool-down when service is down</li>
 * </ul>
 */
public class AuthServiceClient {

    private static final Logger log = LoggerFactory.getLogger(AuthServiceClient.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(5);
    private static final long RETRY_INTERVAL_MS = 30_000;
    private static final long REFRESH_MARGIN_S = 60;

    private final String authUrl;
    private final String clientId;
    private final String clientSecret;
    private final HttpClient httpClient;
    private final ConcurrentHashMap<String, CachedToken> cache = new ConcurrentHashMap<>();
    private volatile boolean available = true;
    private volatile long lastCheckMs = 0;

    public AuthServiceClient(String authUrl, String clientId, String clientSecret) {
        this.authUrl = authUrl.endsWith("/") ? authUrl.substring(0, authUrl.length() - 1) : authUrl;
        this.clientId = clientId;
        this.clientSecret = clientSecret;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(REQUEST_TIMEOUT)
                .build();
        log.info("🔑 AuthServiceClient configured: url={}, clientId={}", this.authUrl, clientId);
    }

    /**
     * Get a valid RS256 token for the target audience.
     *
     * @param audience target service (e.g., "memory-server")
     * @param tenantId tenant context (null → "default")
     * @return access token string, or null if unavailable
     */
    public String getToken(String audience, String tenantId) {
        String cacheKey = audience + ":" + (tenantId != null ? tenantId : "default");

        // Check cache
        CachedToken cached = cache.get(cacheKey);
        if (cached != null && Instant.now().isBefore(cached.expiresAt.minusSeconds(REFRESH_MARGIN_S))) {
            return cached.token;
        }

        // Check if auth service was recently unavailable
        long now = System.currentTimeMillis();
        if (!available && (now - lastCheckMs) < RETRY_INTERVAL_MS) {
            return null;
        }

        // Request new token
        try {
            Map<String, String> payload = new java.util.LinkedHashMap<>();
            payload.put("grant_type", "client_credentials");
            payload.put("client_id", clientId);
            payload.put("client_secret", clientSecret);
            payload.put("audience", audience);
            if (tenantId != null) {
                payload.put("tenant_id", tenantId);
            }

            String body = MAPPER.writeValueAsString(payload);
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(authUrl + "/auth/token"))
                    .header("Content-Type", "application/json")
                    .timeout(REQUEST_TIMEOUT)
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .build();

            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());

            if (response.statusCode() == 200) {
                JsonNode json = MAPPER.readTree(response.body());
                String accessToken = json.get("access_token").asText();
                int expiresIn = json.has("expires_in") ? json.get("expires_in").asInt() : 3600;

                cache.put(cacheKey, new CachedToken(accessToken, Instant.now().plusSeconds(expiresIn)));
                available = true;
                log.debug("🔑 Token obtained for audience={} tenant={}", audience, tenantId);
                return accessToken;
            } else {
                log.warn("🔑 Auth service returned {}: {}", response.statusCode(), response.body());
                available = true; // service is up, just auth failed
                return null;
            }
        } catch (Exception e) {
            log.info("🔑 Auth service unavailable at {}: {}", authUrl, e.getMessage());
            available = false;
            lastCheckMs = now;
            return null;
        }
    }

    /**
     * Clear cached tokens (call on 401 from a backend).
     */
    public void invalidate(String audience) {
        if (audience != null) {
            cache.entrySet().removeIf(e -> e.getKey().startsWith(audience));
        } else {
            cache.clear();
        }
    }

    private record CachedToken(String token, Instant expiresAt) {}
}

