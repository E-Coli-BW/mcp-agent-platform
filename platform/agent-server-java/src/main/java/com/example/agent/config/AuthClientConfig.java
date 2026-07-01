package com.example.agent.config;

import com.example.agent.tools.AuthServiceClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Configures the centralized AuthServiceClient bean.
 *
 * <p>If {@code agent.auth-service-url} is set (or AUTH_SERVICE_URL env),
 * creates a real client. Otherwise, returns a no-op client that always
 * falls through to the HMAC fallback in McpRestClient.</p>
 */
@Configuration
public class AuthClientConfig {

    private static final Logger log = LoggerFactory.getLogger(AuthClientConfig.class);

    @Bean
    public AuthServiceClient authServiceClient(AgentProperties properties) {
        String authUrl = properties.authServiceUrl();
        if (authUrl == null || authUrl.isBlank()) {
            log.info("🔑 No auth-service-url configured — using HMAC-only auth");
            return null;
        }

        String clientId = System.getenv().getOrDefault("AUTH_CLIENT_ID", "agent-server");
        String clientSecret = System.getenv().getOrDefault("AUTH_CLIENT_SECRET", "agent-secret");

        return new AuthServiceClient(authUrl, clientId, clientSecret);
    }
}

