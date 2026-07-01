package com.example.agent.security;

import com.example.agent.config.AgentProperties;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.reactive.EnableWebFluxSecurity;
import org.springframework.security.config.web.server.SecurityWebFiltersOrder;
import org.springframework.security.config.web.server.ServerHttpSecurity;
import org.springframework.security.web.server.SecurityWebFilterChain;

import java.util.List;

/**
 * WebFlux security configuration with reactive JWT filter.
 *
 * <p>Cannot extend mcp-common's {@code McpSecurityConfigBase} because that uses
 * Servlet-based {@code HttpSecurity}. This module uses WebFlux's
 * {@code ServerHttpSecurity}. The reactive JWT filter provides the same
 * RS256 (JWKS) + HMAC authentication strategy.</p>
 */
@Configuration
@EnableWebFluxSecurity
public class SecurityConfig {

    private final AgentProperties properties;

    @Value("${mcp.security.jwks-url:#{null}}")
    private String jwksUrl;

    public SecurityConfig(AgentProperties properties) {
        this.properties = properties;
    }

    @Bean
    public SecurityWebFilterChain securityFilterChain(ServerHttpSecurity http) {
        List<String> openPaths = List.of(
                "/health", "/v1/models", "/api/workspace", "/actuator"
        );

        return http
                .csrf(ServerHttpSecurity.CsrfSpec::disable)
                .httpBasic(ServerHttpSecurity.HttpBasicSpec::disable)
                .formLogin(ServerHttpSecurity.FormLoginSpec::disable)
                .addFilterAt(
                        new ReactiveJwtAuthFilter(properties.jwtSecret(), jwksUrl, openPaths),
                        SecurityWebFiltersOrder.AUTHENTICATION
                )
                .authorizeExchange(auth -> auth
                        .pathMatchers("/health", "/v1/models").permitAll()
                        .pathMatchers("/actuator/health", "/actuator/info").permitAll()
                        .pathMatchers("/api/workspace/**").permitAll()
                        .anyExchange().authenticated()
                )
                .build();
    }
}
