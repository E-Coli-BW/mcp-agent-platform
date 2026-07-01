package com.example.agent.security;

import com.example.mcp.common.security.TenantContext;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
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

/**
 * Reactive JWT authentication filter for WebFlux.
 *
 * <p>Equivalent to mcp-common's {@code JwtAuthFilter} but for the reactive stack.
 * mcp-common uses Servlet's {@code OncePerRequestFilter} which is incompatible
 * with WebFlux. This filter provides the same RS256 (JWKS) + HMAC (fallback)
 * verification strategy.</p>
 *
 * <p>Sets {@link TenantContext} from the {@code tenant_id} JWT claim.</p>
 */
public class ReactiveJwtAuthFilter implements WebFilter {

    private static final Logger log = LoggerFactory.getLogger(ReactiveJwtAuthFilter.class);

    private final SecretKey hmacKey;
    private final String jwksUrl;
    private final List<String> openPaths;
    private volatile PublicKey rsaPublicKey;
    private volatile long keyFetchedAt;

    /**
     * @param secret    HMAC shared secret (legacy fallback)
     * @param jwksUrl   Auth service JWKS URL (null = HMAC only)
     * @param openPaths paths that don't require authentication
     */
    public ReactiveJwtAuthFilter(String secret, String jwksUrl, List<String> openPaths) {
        byte[] keyBytes = secret.getBytes(StandardCharsets.UTF_8);
        if (keyBytes.length < 32) {
            byte[] padded = new byte[32];
            System.arraycopy(keyBytes, 0, padded, 0, keyBytes.length);
            keyBytes = padded;
        }
        this.hmacKey = Keys.hmacShaKeyFor(keyBytes);
        this.jwksUrl = jwksUrl;
        this.openPaths = openPaths;
    }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, WebFilterChain chain) {
        String path = exchange.getRequest().getPath().value();
        String header = exchange.getRequest().getHeaders().getFirst(HttpHeaders.AUTHORIZATION);

        if (openPaths.stream().anyMatch(path::startsWith)) {
            Claims claims = verifyOptionalToken(header);
            return applyTenantContext(exchange, chain.filter(exchange), claims, false);
        }

        if (header == null || !header.startsWith("Bearer ")) {
            exchange.getResponse().setStatusCode(HttpStatus.UNAUTHORIZED);
            return exchange.getResponse().setComplete();
        }

        Claims claims = verifyToken(header.substring(7));
        if (claims == null) {
            exchange.getResponse().setStatusCode(HttpStatus.UNAUTHORIZED);
            return exchange.getResponse().setComplete();
        }

        return applyTenantContext(exchange, chain.filter(exchange), claims, true);
    }

    private Claims verifyOptionalToken(String header) {
        if (header == null || !header.startsWith("Bearer ")) {
            return null;
        }
        return verifyToken(header.substring(7));
    }

    private Mono<Void> applyTenantContext(
            ServerWebExchange exchange,
            Mono<Void> chainResult,
            Claims claims,
            boolean useThreadLocal) {
        String tenantId = resolveTenantId(claims);
        exchange.getAttributes().put("tenantId", tenantId);
        Mono<Void> result = chainResult.contextWrite(ctx -> ctx.put("tenantId", tenantId));
        if (claims != null) {
            result = result.contextWrite(ReactiveSecurityContextHolder.withSecurityContext(
                    Mono.just(new SecurityContextImpl(buildAuthentication(claims)))));
        }
        if (!useThreadLocal) {
            return result;
        }
        return result
                .doOnSubscribe(sub -> TenantContext.set(tenantId))
                .doFinally(signal -> TenantContext.clear());
    }

    private UsernamePasswordAuthenticationToken buildAuthentication(Claims claims) {
        return new UsernamePasswordAuthenticationToken(
                claims.getSubject(),
                null,
                List.of(new SimpleGrantedAuthority("ROLE_SERVICE"))
        );
    }

    private String resolveTenantId(Claims claims) {
        if (claims == null) {
            return "default";
        }
        String tenantId = claims.get("tenant_id", String.class);
        if (tenantId == null) {
            tenantId = claims.getSubject();
        }
        return tenantId != null ? tenantId : "default";
    }

    private Claims verifyToken(String token) {
        // Strategy 1: RS256 via JWKS
        if (jwksUrl != null) {
            try {
                PublicKey pk = getRsaPublicKey();
                if (pk != null) {
                    return Jwts.parser().verifyWith(pk).build()
                            .parseSignedClaims(token).getPayload();
                }
            } catch (Exception e) {
                log.debug("RS256 verification failed, trying HMAC: {}", e.getMessage());
            }
        }

        // Strategy 2: HMAC fallback
        try {
            return Jwts.parser().verifyWith(hmacKey).build()
                    .parseSignedClaims(token).getPayload();
        } catch (Exception e) {
            log.debug("HMAC verification also failed: {}", e.getMessage());
            return null;
        }
    }

    private PublicKey getRsaPublicKey() {
        long now = System.currentTimeMillis();
        if (rsaPublicKey != null && (now - keyFetchedAt) < 300_000) {
            return rsaPublicKey;
        }
        try {
            var client = HttpClient.newHttpClient();
            var request = HttpRequest.newBuilder().uri(URI.create(jwksUrl)).GET().build();
            var response = client.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) return rsaPublicKey;

            String body = response.body();
            String n = extractJsonField(body, "n");
            String e = extractJsonField(body, "e");
            if (n != null && e != null) {
                byte[] nBytes = Base64.getUrlDecoder().decode(n);
                byte[] eBytes = Base64.getUrlDecoder().decode(e);
                rsaPublicKey = KeyFactory.getInstance("RSA").generatePublic(
                        new RSAPublicKeySpec(new BigInteger(1, nBytes), new BigInteger(1, eBytes)));
                keyFetchedAt = now;
            }
        } catch (Exception ex) {
            log.warn("Failed to fetch JWKS from {}: {}", jwksUrl, ex.getMessage());
        }
        return rsaPublicKey;
    }

    private String extractJsonField(String json, String field) {
        String search = "\"" + field + "\":\"";
        int start = json.indexOf(search);
        if (start < 0) return null;
        start += search.length();
        int end = json.indexOf("\"", start);
        return end > start ? json.substring(start, end) : null;
    }
}
