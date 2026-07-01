package com.example.auth.config;

import com.example.auth.model.AuthPolicy;
import com.example.auth.service.TokenService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.context.annotation.Profile;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * Registers default clients and policies on startup for dev/test profiles.
 */
@Component
@Profile({"dev", "test"})
public class DefaultClientInitializer implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(DefaultClientInitializer.class);

    private final TokenService tokenService;
    private final Environment environment;

    public DefaultClientInitializer(TokenService tokenService, Environment environment) {
        this.tokenService = tokenService;
        this.environment = environment;
    }

    @Override
    public void run(String... args) {
        log.warn("⚠️ DefaultClientInitializer is active (profiles={}). Dev-only default secrets are being seeded. DO NOT run this profile in production.",
                String.join(",", environment.getActiveProfiles()));

        // Register clients
        registerIfAbsent("agent-server", "agent-secret",
                "Python Agent Server", "memory:*,filesearch:*,code:*", null);
        registerIfAbsent("web-frontend", "web-secret",
                "Web Frontend", "chat:*", null);
        registerIfAbsent("admin-cli", "admin-secret",
                "Admin CLI", "*", null);

        // Seed policies (actor, type, audience, tenant, permissions)
        seedPolicy("agent-server", AuthPolicy.ActorType.SERVICE,
                "memory-server", "*", "MEMORY_READ,MEMORY_WRITE,MEMORY_DELETE");
        seedPolicy("agent-server", AuthPolicy.ActorType.SERVICE,
                "filesearch-server", "*", "FS_READ");
        seedPolicy("agent-server", AuthPolicy.ActorType.SERVICE,
                "codeexec-server", "*", "CODE_EXEC");
        seedPolicy("admin-cli", AuthPolicy.ActorType.SERVICE,
                "*", "*", "*");

        log.info("✅ Default auth clients and policies registered");
    }

    private void registerIfAbsent(String clientId, String secret,
                                   String name, String scopes, String tenantId) {
        try {
            tokenService.registerClient(clientId, secret, name, scopes, tenantId);
            log.info("  Registered client: {}", clientId);
        } catch (TokenService.AuthException e) {
            // Already exists
        }
    }

    private void seedPolicy(String actor, AuthPolicy.ActorType actorType,
                             String audience, String tenant, String permissions) {
        try {
            tokenService.createPolicy(actor, actorType, audience, tenant, permissions);
            log.info("  Policy: {} → {} (tenant={}, perms={})", actor, audience, tenant, permissions);
        } catch (Exception e) {
            // Already exists or constraint violation
        }
    }
}
