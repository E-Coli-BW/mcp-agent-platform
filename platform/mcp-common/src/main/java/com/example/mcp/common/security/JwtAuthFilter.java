package com.example.mcp.common.security;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

import javax.crypto.SecretKey;
import java.io.IOException;
import java.math.BigInteger;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.PublicKey;
import java.security.spec.RSAPublicKeySpec;
import java.util.Base64;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * JWT authentication filter — supports RS256 (JWKS) + HMAC (legacy fallback).
 *
 * Authentication strategy:
 * 1. If JWKS URL is configured, try RS256 verification first
 * 2. Fall back to HMAC (shared secret) if RS256 fails or JWKS unavailable
 * 3. Extract tenant_id + permissions from JWT claims
 */
public class JwtAuthFilter extends OncePerRequestFilter {
    private static final Logger log = LoggerFactory.getLogger(JwtAuthFilter.class);

    private final SecretKey hmacKey;          // HMAC fallback — null when RS256-only
    private final String jwksUrl;             // Auth service JWKS endpoint

    /**
     * Cached JWKS keys, keyed by {@code kid} (JWT header "kid" claim).
     *
     * Why a Map (not a single PublicKey)?
     *   Key rotation works by publishing both the OLD and NEW key in JWKS for
     *   the duration of the rollover window. Tokens issued before the rollover
     *   are signed by OLD; tokens issued after are signed by NEW; both must
     *   verify until OLD's last token expires. Picking the key by JWT header
     *   {@code kid} matches exactly the key that signed the token.
     *
     * The old implementation used naive {@code indexOf("\"n\":")} JSON parsing
     * and stored a single PublicKey — it would silently pin to the first key
     * in the JWKS array and reject every token signed with the second key.
     */
    private volatile Map<String, PublicKey> rsaKeys = Collections.emptyMap();
    private volatile long keyFetchedAt;       // When was the key map last fetched

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(java.time.Duration.ofSeconds(3))
            .build();

    /**
     * @param secret  HMAC shared secret. Pass {@code null} or blank to disable HMAC entirely
     *                and require RS256 / JWKS verification. (C3 fix: do NOT fabricate a
     *                placeholder secret — that allowed attackers to forge HS256 tokens.)
     * @param jwksUrl Auth service JWKS URL (null = HMAC only). When non-null and {@code secret}
     *                is null/blank, filter operates in RS256-only mode.
     */
    public JwtAuthFilter(String secret, String jwksUrl) {
        if (secret == null || secret.isBlank()) {
            // RS256-only mode — no HMAC fallback. C3 fix.
            this.hmacKey = null;
            if (jwksUrl == null || jwksUrl.isBlank()) {
                throw new IllegalArgumentException(
                        "JwtAuthFilter requires either a non-blank HMAC secret or a JWKS URL.");
            }
            log.info("🔒 JWT filter in RS256-only mode (JWKS: {})", jwksUrl);
        } else {
            byte[] keyBytes = secret.getBytes(StandardCharsets.UTF_8);
            if (keyBytes.length < 32) {
                log.warn("⚠️ JWT secret is only {} bytes — padding to 32.", keyBytes.length);
                if (keyBytes.length < 8) {
                    throw new IllegalArgumentException("JWT secret must be at least 8 bytes.");
                }
                byte[] padded = new byte[32];
                System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
                keyBytes = padded;
            }
            this.hmacKey = Keys.hmacShaKeyFor(keyBytes);
        }
        this.jwksUrl = (jwksUrl == null || jwksUrl.isBlank()) ? null : jwksUrl;
        if (this.jwksUrl != null && this.hmacKey != null) {
            log.info("🔑 JWT filter configured with both JWKS ({}) and HMAC fallback", this.jwksUrl);
        }
    }

    /** Legacy constructor — HMAC only */
    public JwtAuthFilter(String secret) {
        this(secret, null);
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain filterChain) throws ServletException, IOException {
        try {
            String header = request.getHeader("Authorization");
            if (header != null && header.startsWith("Bearer ")) {
                String token = header.substring(7);
                Claims claims = verifyToken(token);
                if (claims != null) {
                    String tenantId = claims.get("tenant_id", String.class);
                    if (tenantId == null) tenantId = claims.getSubject();
                    TenantContext.set(tenantId);

                    List<SimpleGrantedAuthority> authorities = extractAuthorities(claims);

                    SecurityContextHolder.getContext().setAuthentication(
                            new UsernamePasswordAuthenticationToken(
                                    claims.getSubject(), null, authorities));
                }
            }
            // NOTE: If no token is present, no Authentication is set in SecurityContext.
            // Spring Security's authorizeHttpRequests will reject the request with 401
            // for any path that requires authentication. This is by design — the filter
            // only PARSES tokens, it does not ENFORCE auth (that's Spring Security's job).
        } catch (Exception e) {
            log.debug("JWT validation failed: {}", e.getMessage());
        }
        try {
            filterChain.doFilter(request, response);
        } finally {
            TenantContext.clear();
        }
    }

    /**
     * Verify token — tries RS256 (JWKS) first, falls back to HMAC if configured.
     * When operating in RS256-only mode ({@code hmacKey == null}), HS256 tokens are
     * rejected outright — closing the C3 auth-bypass vector.
     *
     * RS256 verification routes by the JWT header {@code kid} claim so multiple
     * keys can coexist during key rotation (see {@link #rsaKeys} javadoc).
     */
    private Claims verifyToken(String token) {
        // Strategy 1: RS256 via JWKS
        if (jwksUrl != null) {
            try {
                PublicKey pk = resolveRsaKey(token);
                if (pk != null) {
                    return Jwts.parser().verifyWith(pk).build()
                            .parseSignedClaims(token).getPayload();
                }
            } catch (Exception e) {
                log.debug("RS256 verification failed{}: {}",
                        hmacKey != null ? ", trying HMAC" : " (RS256-only mode)", e.getMessage());
            }
        }

        // Strategy 2: HMAC fallback — only if explicitly configured
        if (hmacKey == null) {
            return null;
        }
        try {
            return Jwts.parser().verifyWith(hmacKey).build()
                    .parseSignedClaims(token).getPayload();
        } catch (Exception e) {
            log.debug("HMAC verification also failed: {}", e.getMessage());
            return null;
        }
    }

    /**
     * Extract authorities from JWT claims.
     * Supports both "permissions" (policy-based) and "roles" (legacy) claims.
     */
    private List<SimpleGrantedAuthority> extractAuthorities(Claims claims) {
        // Try "permissions" first (from auth service policy)
        @SuppressWarnings("unchecked")
        List<String> perms = claims.get("permissions", List.class);
        if (perms != null && !perms.isEmpty()) {
            var authorities = perms.stream()
                    .map(SimpleGrantedAuthority::new)
                    .collect(Collectors.toList());
            authorities.add(new SimpleGrantedAuthority("ROLE_SERVICE"));
            return authorities;
        }

        // Fallback to "roles" claim
        @SuppressWarnings("unchecked")
        List<String> roles = claims.get("roles", List.class);
        if (roles != null && !roles.isEmpty()) {
            return roles.stream()
                    .map(SimpleGrantedAuthority::new)
                    .collect(Collectors.toList());
        }

        return List.of(new SimpleGrantedAuthority("ROLE_SERVICE"));
    }

    /**
     * Pick the RSA public key that signed the given token.
     *
     * <p>Reads the unverified JWT header to get {@code kid}, then looks it up
     * in the cached JWKS map. If {@code kid} is missing or unknown we fall
     * back to "any single key" — which is the legal degenerate case for
     * single-key JWKS responses.</p>
     */
    private PublicKey resolveRsaKey(String token) {
        Map<String, PublicKey> keys = getJwksKeys();
        if (keys.isEmpty()) {
            return null;
        }

        String kid = extractKidFromHeader(token);
        if (kid != null) {
            PublicKey k = keys.get(kid);
            if (k != null) {
                return k;
            }
            // kid in token but not in JWKS — likely a rotation race where
            // the new key hasn't been published yet. Force a refresh and retry.
            log.debug("kid '{}' not in JWKS cache — forcing refresh", kid);
            keyFetchedAt = 0;
            keys = getJwksKeys();
            k = keys.get(kid);
            if (k != null) {
                return k;
            }
            log.warn("Token signed with unknown kid '{}' — refusing", kid);
            return null;
        }

        // No kid in token header — only safe if JWKS has exactly one key.
        if (keys.size() == 1) {
            return keys.values().iterator().next();
        }
        log.warn("JWKS exposes {} keys but token has no 'kid' — cannot pick", keys.size());
        return null;
    }

    /**
     * Decode the JWT header (first segment, base64url JSON) and return the
     * {@code kid} field. Returns null if missing/malformed. The header is
     * parsed UNVERIFIED — we only use it to choose a key; the actual
     * signature check happens in {@link #verifyToken}.
     */
    private String extractKidFromHeader(String token) {
        try {
            int dot = token.indexOf('.');
            if (dot <= 0) return null;
            byte[] headerBytes = Base64.getUrlDecoder().decode(token.substring(0, dot));
            JsonNode header = JSON.readTree(headerBytes);
            JsonNode kid = header.get("kid");
            return kid != null && kid.isTextual() ? kid.asText() : null;
        } catch (Exception e) {
            log.debug("Failed to parse JWT header for kid: {}", e.getMessage());
            return null;
        }
    }

    /**
     * Fetch and cache JWKS keys (kid → PublicKey) from the JWKS endpoint.
     * Re-fetches every 5 minutes; returns the previous map on transient failures.
     */
    private Map<String, PublicKey> getJwksKeys() {
        long now = System.currentTimeMillis();
        Map<String, PublicKey> cached = rsaKeys;
        if (!cached.isEmpty() && (now - keyFetchedAt) < 300_000) { // 5 min cache
            return cached;
        }

        try {
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(jwksUrl))
                    .timeout(java.time.Duration.ofSeconds(3))
                    .GET()
                    .build();
            HttpResponse<String> response = HTTP.send(req, HttpResponse.BodyHandlers.ofString());

            if (response.statusCode() != 200) {
                log.warn("JWKS fetch failed: HTTP {}", response.statusCode());
                return cached; // serve stale rather than 401 all traffic
            }

            Map<String, PublicKey> parsed = parseJwks(response.body());
            if (parsed.isEmpty()) {
                log.warn("JWKS response had no RSA keys; keeping cached set ({} keys)", cached.size());
                return cached;
            }
            rsaKeys = parsed;
            keyFetchedAt = now;
            log.debug("JWKS refreshed from {} — {} key(s): {}",
                    jwksUrl, parsed.size(), parsed.keySet());
            return parsed;
        } catch (Exception ex) {
            log.warn("Failed to fetch JWKS from {}: {}", jwksUrl, ex.getMessage());
            return cached;
        }
    }

    /**
     * Parse a JWKS JSON body using Jackson. Returns kid → PublicKey for every
     * RSA key in the {@code keys} array. Skips non-RSA keys and malformed entries.
     *
     * Package-private so unit tests can exercise it directly.
     */
    static Map<String, PublicKey> parseJwks(String body) throws Exception {
        JsonNode root = JSON.readTree(body);
        JsonNode keysArray = root.get("keys");
        if (keysArray == null || !keysArray.isArray()) {
            return Collections.emptyMap();
        }
        Map<String, PublicKey> result = new HashMap<>();
        KeyFactory rsaFactory = KeyFactory.getInstance("RSA");
        for (JsonNode key : keysArray) {
            JsonNode ktyNode = key.get("kty");
            if (ktyNode == null || !"RSA".equals(ktyNode.asText())) {
                continue;
            }
            JsonNode nNode = key.get("n");
            JsonNode eNode = key.get("e");
            JsonNode kidNode = key.get("kid");
            if (nNode == null || eNode == null) {
                continue;
            }
            try {
                byte[] nBytes = Base64.getUrlDecoder().decode(nNode.asText());
                byte[] eBytes = Base64.getUrlDecoder().decode(eNode.asText());
                RSAPublicKeySpec spec = new RSAPublicKeySpec(
                        new BigInteger(1, nBytes), new BigInteger(1, eBytes));
                PublicKey pk = rsaFactory.generatePublic(spec);
                // Keys without kid get a synthesised one so a single-key JWKS
                // still works (resolveRsaKey handles the no-kid token path).
                String kid = kidNode != null && kidNode.isTextual()
                        ? kidNode.asText() : "default";
                result.put(kid, pk);
            } catch (Exception e) {
                // Skip the bad key but keep the others.
                // Better partial JWKS than total failure during key rotation.
                continue;
            }
        }
        return result;
    }
}
