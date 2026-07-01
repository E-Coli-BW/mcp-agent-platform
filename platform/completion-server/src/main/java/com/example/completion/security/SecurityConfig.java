package com.example.completion.security;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity;
import org.springframework.security.config.web.server.SecurityWebFiltersOrder;
import org.springframework.security.config.web.server.ServerHttpSecurity;
import org.springframework.security.web.server.SecurityWebFilterChain;

import java.util.List;

@Configuration
@EnableWebFluxSecurity
public class SecurityConfig {

    private final String jwtSecret;
    private final String jwksUrl;

    public SecurityConfig(
            @Value("${mcp.security.jwt-secret:}") String jwtSecret,
            @Value("${mcp.security.jwks-url:#{null}}") String jwksUrl) {
        this.jwtSecret = jwtSecret;
        this.jwksUrl = jwksUrl == null || jwksUrl.isBlank() ? null : jwksUrl;
    }

    @Bean
    public SecurityWebFilterChain securityWebFilterChain(ServerHttpSecurity http) {
        boolean hasSecret = jwtSecret != null && !jwtSecret.isBlank();
        boolean hasJwks = jwksUrl != null && !jwksUrl.isBlank();
        if (!hasSecret && !hasJwks) {
            throw new IllegalStateException(
                    "SECURITY: No JWT secret (mcp.security.jwt-secret) or JWKS URL "
                            + "(mcp.security.jwks-url) configured. At least one is required.");
        }

        List<String> openPaths = List.of("/actuator/health", "/actuator/info");
        return http
                .csrf(ServerHttpSecurity.CsrfSpec::disable)
                .httpBasic(ServerHttpSecurity.HttpBasicSpec::disable)
                .formLogin(ServerHttpSecurity.FormLoginSpec::disable)
                .addFilterAt(
                        new ReactiveJwtAuthFilter(hasSecret ? jwtSecret : null, jwksUrl, openPaths),
                        SecurityWebFiltersOrder.AUTHENTICATION)
                .authorizeExchange(auth -> auth
                        .pathMatchers("/actuator/health", "/actuator/info").permitAll()
                        .anyExchange().authenticated())
                .build();
    }
}
