package com.example.mcp.common.security;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

/**
 * Base security configuration for MCP services.
 * Services extend this and add their own permitAll paths if needed.
 *
 * Usage in service:
 * <pre>
 * @Configuration @EnableWebSecurity
 * public class MySecurityConfig extends McpSecurityConfigBase { }
 * </pre>
 */
public abstract class McpSecurityConfigBase {

    @Value("${mcp.security.jwt-secret:}")
    private String jwtSecret;

    @Value("${mcp.security.jwks-url:#{null}}")
    private String jwksUrl;

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        // C3 fix: never fabricate a placeholder HMAC secret. If JWKS is configured
        // and no HMAC secret is set, the filter runs in RS256-only mode and refuses
        // HS256 tokens entirely — closing the auth-bypass-by-forged-HS256 vector.
        boolean hasSecret = jwtSecret != null && !jwtSecret.isBlank();
        boolean hasJwks = jwksUrl != null && !jwksUrl.isBlank();
        if (!hasSecret && !hasJwks) {
            throw new IllegalStateException(
                    "SECURITY: No JWT secret (mcp.security.jwt-secret) or JWKS URL "
                    + "(mcp.security.jwks-url) configured. At least one is required.");
        }
        String effectiveSecret = hasSecret ? jwtSecret : null;
        return http
                .csrf(csrf -> csrf.disable())
                .httpBasic(basic -> basic.disable())
                .formLogin(form -> form.disable())
                .sessionManagement(sm -> sm.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> configureAuth(auth))
                .addFilterBefore(new JwtAuthFilter(effectiveSecret, jwksUrl), UsernamePasswordAuthenticationFilter.class)
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint((req, resp, authEx) -> {
                            resp.setStatus(401);
                            resp.setContentType("application/json");
                            resp.getWriter().write("{\"error\":\"Authentication required\"}");
                        }))
                .build();
    }

    /**
     * Override to add service-specific permitAll paths.
     * Default: health, dev, sse, mcp are open; everything else requires auth.
     */
    protected void configureAuth(
            org.springframework.security.config.annotation.web.configurers.AuthorizeHttpRequestsConfigurer<HttpSecurity>.AuthorizationManagerRequestMatcherRegistry auth) {
        auth
                .requestMatchers("/actuator/health", "/actuator/info").permitAll()
                .requestMatchers("/sse/**", "/mcp/**").permitAll()
                .requestMatchers("/api/**").hasAuthority("ROLE_SERVICE")
                .anyRequest().denyAll();
    }
}
