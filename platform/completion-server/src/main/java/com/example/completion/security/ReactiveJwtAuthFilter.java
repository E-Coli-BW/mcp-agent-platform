package com.example.completion.security;

import com.example.mcp.common.security.TenantContext;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.ReactiveSecurityContextHolder;
import org.springframework.security.core.context.SecurityContextImpl;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.web.server.WebFilter;
import org.springframework.web.server.WebFilterChain;
import reactor.core.publisher.Mono;

import javax.crypto.SecretKey;
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
import java.util.List;
import java.util.stream.Collectors;

public class ReactiveJwtAuthFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(ReactiveJwtAuthFilter.class);

    private final SecretKey hmacKey;
    private final String jwksUrl;
    private final List<String> openPaths;
    private volatile PublicKey rsaPublicKey;
    private volatile long keyFetchedAt;

    public ReactiveJwtAuthFilter(String secret, String jwksUrl, List<String> openPaths) {
        if (secret == null || secret.isBlank()) {
            this.hmacKey = null;
            if (jwksUrl == null || jwksUrl.isBlank()) {
                throw new IllegalArgumentException(
                        "ReactiveJwtAuthFilter requires either a non-blank HMAC secret or a JWKS URL.");
            }
            log.info("JWT filter in RS256-only mode (JWKS: {})", jwksUrl);
        } else {
            byte[] keyBytes = secret.getBytes(StandardCharsets.UTF_8);
            if (keyBytes.length < 32) {
                log.warn("JWT secret is only {} bytes, padding to 32.", keyBytes.length);
                if (keyBytes.length < 8) {
                    throw new IllegalArgumentException("JWT secret must be at least 8 bytes.");
                }
                byte[] padded = new byte[32];
                System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
                keyBytes = padded;
            }
            this.hmacKey = Keys.hmacShaKeyFor(keyBytes);
        }
        this.jwksUrl = jwksUrl == null || jwksUrl.isBlank() ? null : jwksUrl;
        this.openPaths = List.copyOf(openPaths);
        if (this.jwksUrl != null && this.hmacKey != null) {
            log.info("JWT filter configured with both JWKS ({}) and HMAC fallback", this.jwksUrl);
        }
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String path = exchange.getRequest().getPath().value();
        if (isOpenPath(path)) {
            return chain.filter(exchange);
        }

        String header = exchange.getRequest().getHeaders().getFirst(HttpHeaders.AUTHORIZATION);
        if (header == null || !header.startsWith("Bearer ")) {
            return chain.filter(exchange);
        }

        Claims claims = verifyToken(header.substring(7));
        if (claims == null) {
            return chain.filter(exchange);
        }

        String subject = claims.getSubject() != null ? claims.getSubject() : "service";
        String tenantId = claims.get("tenant_id", String.class);
        if (tenantId == null || tenantId.isBlank()) {
            tenantId = subject;
        }

        var authentication = new UsernamePasswordAuthenticationToken(
                subject,
                null,
                extractAuthorities(claims));
        var securityContext = new SecurityContextImpl(authentication);

        String resolvedTenantId = tenantId;
        return chain.filter(exchange)
                .contextWrite(ReactiveSecurityContextHolder.withSecurityContext(Mono.just(securityContext)))
                .doOnSubscribe(subscription -> TenantContext.set(resolvedTenantId))
                .doFinally(signalType -> TenantContext.clear());
    }

    private boolean isOpenPath(String path) {
        return openPaths.stream().anyMatch(path::equals);
    }

    private Claims verifyToken(String token) {
        if (jwksUrl != null) {
            try {
                PublicKey publicKey = getRsaPublicKey();
                if (publicKey != null) {
                    return Jwts.parser().verifyWith(publicKey).build()
                            .parseSignedClaims(token).getPayload();
                }
            } catch (Exception e) {
                log.debug("RS256 verification failed{}: {}",
                        hmacKey != null ? ", trying HMAC" : " (RS256-only mode)", e.getMessage());
            }
        }

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

    private List<SimpleGrantedAuthority> extractAuthorities(Claims claims) {
        @SuppressWarnings("unchecked")
        List<String> permissions = claims.get("permissions", List.class);
        if (permissions != null && !permissions.isEmpty()) {
            var authorities = permissions.stream()
                    .map(SimpleGrantedAuthority::new)
                    .collect(Collectors.toList());
            authorities.add(new SimpleGrantedAuthority("ROLE_SERVICE"));
            return authorities;
        }

        @SuppressWarnings("unchecked")
        List<String> roles = claims.get("roles", List.class);
        if (roles != null && !roles.isEmpty()) {
            return roles.stream()
                    .map(SimpleGrantedAuthority::new)
                    .collect(Collectors.toList());
        }

        return List.of(new SimpleGrantedAuthority("ROLE_SERVICE"));
    }

    private PublicKey getRsaPublicKey() {
        long now = System.currentTimeMillis();
        if (rsaPublicKey != null && (now - keyFetchedAt) < 300_000) {
            return rsaPublicKey;
        }

        try {
            HttpClient client = HttpClient.newHttpClient();
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(jwksUrl))
                    .GET()
                    .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) {
                log.warn("JWKS fetch failed: HTTP {}", response.statusCode());
                return rsaPublicKey;
            }

            String body = response.body();
            String modulus = extractJsonField(body, "n");
            String exponent = extractJsonField(body, "e");
            if (modulus != null && exponent != null) {
                byte[] modulusBytes = Base64.getUrlDecoder().decode(modulus);
                byte[] exponentBytes = Base64.getUrlDecoder().decode(exponent);
                RSAPublicKeySpec spec = new RSAPublicKeySpec(
                        new BigInteger(1, modulusBytes),
                        new BigInteger(1, exponentBytes));
                rsaPublicKey = KeyFactory.getInstance("RSA").generatePublic(spec);
                keyFetchedAt = now;
                log.debug("JWKS public key refreshed from {}", jwksUrl);
            }
        } catch (Exception e) {
            log.warn("Failed to fetch JWKS from {}: {}", jwksUrl, e.getMessage());
        }
        return rsaPublicKey;
    }

    private String extractJsonField(String json, String field) {
        String search = "\"" + field + "\":\"";
        int start = json.indexOf(search);
        if (start < 0) {
            return null;
        }
        start += search.length();
        int end = json.indexOf('"', start);
        return end > start ? json.substring(start, end) : null;
    }
}
