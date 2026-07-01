package com.example.mcp.common.security;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;
import java.util.Map;

/**
 * Dev-only JWT token generator. Shared across all MCP services.
 * SECURITY: Only active when spring.profiles.active=dev.
 * In production, this bean is not created — the endpoint does not exist.
 */
@RestController
@org.springframework.context.annotation.Profile("dev")
public class DevTokenController {

    @Value("${mcp.security.jwt-secret:default-dev-secret-change-in-production}")
    private String jwtSecret;

    @GetMapping("/dev/token")
    public Map<String, String> generateToken(
            @RequestParam(value = "tenant", defaultValue = "default-tenant") String tenant) {
        var key = Keys.hmacShaKeyFor(jwtSecret.getBytes(StandardCharsets.UTF_8));
        String token = Jwts.builder()
                .subject(tenant).claim("tenant_id", tenant)
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plus(24, ChronoUnit.HOURS)))
                .signWith(key).compact();
        return Map.of("token", token, "tenant", tenant);
    }
}
