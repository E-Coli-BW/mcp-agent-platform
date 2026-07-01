package com.example.auth.service;

import com.example.auth.config.AuthTokenProperties;
import com.example.auth.model.AuthClient;
import com.example.auth.model.AuthPolicy;
import com.example.auth.repository.AuthClientRepository;
import com.example.auth.repository.AuthPolicyRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Jwts;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.*;

/**
 * Issues JWT tokens for authenticated clients.
 *
 * Supports:
 * - client_credentials grant (service-to-service M2M)
 * - RS256 signing (asymmetric — only this service has the private key)
 * - Audience scoping (token valid only for specified target service)
 * - Tenant-aware tokens
 */
@Service
public class TokenService {

    private static final Logger log = LoggerFactory.getLogger(TokenService.class);

    private final AuthClientRepository clientRepo;
    private final AuthPolicyRepository policyRepo;
    private final RsaKeyManager keyManager;
    private final TokenBlacklistService tokenBlacklistService;
    private final AuthTokenProperties tokenProperties;
    private final BCryptPasswordEncoder passwordEncoder = new BCryptPasswordEncoder();

    /** Track failed auth attempts per client_id to prevent brute-force */
    private final java.util.concurrent.ConcurrentHashMap<String, FailedAttempt> failedAttempts
            = new java.util.concurrent.ConcurrentHashMap<>();

    public TokenService(AuthClientRepository clientRepo, AuthPolicyRepository policyRepo,
                        RsaKeyManager keyManager, TokenBlacklistService tokenBlacklistService,
                        AuthTokenProperties tokenProperties) {
        this.clientRepo = clientRepo;
        this.policyRepo = policyRepo;
        this.keyManager = keyManager;
        this.tokenBlacklistService = tokenBlacklistService;
        this.tokenProperties = tokenProperties;
    }

    /**
     * Check if a token JTI is blacklisted.
     */
    public boolean isBlacklisted(String jti) {
        return tokenBlacklistService.isBlacklisted(jti);
    }

    /**
     * Authenticate a client and issue a JWT token.
     *
     * @param clientId     registered client ID
     * @param clientSecret plaintext secret (verified against bcrypt hash)
     * @param audience     target service this token is for (e.g., "memory-server")
     * @param tenantId     tenant context (null for cross-tenant service accounts)
     * @return signed JWT token
     * @throws AuthException if authentication fails
     */
    public TokenResponse issueToken(String clientId, String clientSecret,
                                     String audience, String tenantId) {
        // 0. Rate limit check — prevent brute-force on client_secret
        checkRateLimit(clientId);

        // 1. Find client
        AuthClient client = clientRepo.findByClientId(clientId)
                .orElseThrow(() -> new AuthException("Invalid client_id"));

        // 2. Verify enabled
        if (!client.isEnabled()) {
            throw new AuthException("Client is disabled");
        }

        // 3. Verify secret
        if (!passwordEncoder.matches(clientSecret, client.getClientSecret())) {
            log.warn("Authentication failed for client '{}': bad secret", clientId);
            recordFailedAttempt(clientId);
            throw new AuthException("Invalid client_secret");
        }

        // Clear failed attempts on success
        failedAttempts.remove(clientId);

        if (!canMintForTenant(clientId, tenantId, client.getTenantId())) {
            throw new AuthException("Client not authorized to mint tokens for tenant '" + tenantId + "'");
        }

        // 4. Resolve tenant
        String effectiveTenant = tenantId != null ? tenantId
                : (client.getTenantId() != null ? client.getTenantId() : "default");

        // 5. Policy lookup — what permissions does this actor have for this audience+tenant?
        List<AuthPolicy> policies = policyRepo.findMatchingPolicies(clientId, audience, effectiveTenant);
        List<String> permissions;
        String actorType;

        if (!policies.isEmpty()) {
            // Use policy-defined permissions (prefer exact tenant match over wildcard)
            AuthPolicy bestMatch = policies.stream()
                    .filter(p -> p.getTenantId().equals(effectiveTenant))
                    .findFirst()
                    .orElse(policies.get(0));
            permissions = bestMatch.getPermissionList();
            actorType = bestMatch.getActorType().name();
        } else {
            // No policy → fallback to client scopes (backward compatible)
            permissions = buildRoles(client.getScopes(), audience);
            actorType = "SERVICE";
        }

        // 6. Sign JWT with RS256
        Instant now = Instant.now();
        String token = Jwts.builder()
                .header().keyId(keyManager.getKeyId()).and()
                .issuer("mcp-auth-service")
                .subject(clientId)
                .audience().add(audience).and()
                .claim("tenant_id", effectiveTenant)
                .claim("sub_type", actorType)
                .claim("permissions", permissions)
                .id(UUID.randomUUID().toString())
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plus(tokenProperties.getAccessTtlSeconds(), ChronoUnit.SECONDS)))
                .signWith(keyManager.getPrivateKey())
                .compact();

        log.info("Token issued: client={}, tenant={}, audience={}, permissions={}",
                clientId, effectiveTenant, audience, permissions);

        return new TokenResponse(token, "Bearer", tokenProperties.getAccessTtlSeconds());
    }

    /**
     * Register a new client. Used during setup or via admin API.
     */
    public AuthClient registerClient(String clientId, String rawSecret,
                                      String clientName, String scopes, String tenantId) {
        if (clientRepo.findByClientId(clientId).isPresent()) {
            throw new DuplicateClientException("Client already exists: " + clientId);
        }
        String hashedSecret = passwordEncoder.encode(rawSecret);
        AuthClient client = new AuthClient(clientId, hashedSecret, clientName, scopes, tenantId);
        return clientRepo.save(client);
    }

    private List<String> buildRoles(String scopes, String audience) {
        if (scopes == null || scopes.isBlank()) return List.of("SERVICE");
        List<String> roles = new ArrayList<>();
        roles.add("SERVICE");
        for (String scope : scopes.split(",")) {
            String s = scope.trim();
            // "memory:*" → "MEMORY_READ", "MEMORY_WRITE"
            // "code:exec" → "CODE_EXEC"
            roles.add(s.replace(":", "_").replace("*", "ALL").toUpperCase());
        }
        return roles;
    }

    private boolean canMintForTenant(String clientId, String requestedTenant, String ownTenant) {
        // Case 1: no specific tenant requested → always OK
        if (requestedTenant == null) {
            return true;
        }
        // Case 2: requesting your own tenant → always OK
        if (requestedTenant.equals(ownTenant)) {
            return true;
        }
        // Case 3: client has NO tenant constraint (ownTenant == null) → cross-tenant
        // service account by design (e.g. agent-server serves all tenants). Allow.
        // Without this, the seeded "agent-server" client (ownTenant=null) is unable
        // to mint a token for any specific tenant — it always falls through to the
        // policy-check branch, which then needs an explicit "auth-service" audience
        // policy that the DefaultClientInitializer never creates. Result: 401
        // "invalid_client" for every request that carries tenant_id.
        if (ownTenant == null) {
            return true;
        }
        // Case 4: client is bound to tenant X but asks for tenant Y → needs explicit
        // MINT_CROSS_TENANT permission on the auth-service audience.
        List<AuthPolicy> policies = policyRepo.findMatchingPolicies(clientId, "auth-service", requestedTenant);
        return policies.stream()
                .flatMap(policy -> policy.getPermissionList().stream())
                .anyMatch(permission -> permission.equals("MINT_CROSS_TENANT") || permission.equals("*"));
    }

    // ── DTOs ─────────────────────────────────────────────────

    public record TokenResponse(String accessToken, String tokenType, long expiresIn) {}

    public static class AuthException extends RuntimeException {
        public AuthException(String message) { super(message); }
    }

    public static class DuplicateClientException extends RuntimeException {
        public DuplicateClientException(String message) { super(message); }
    }

    /**
     * Create a policy. Used during setup or via admin API.
     */
    public AuthPolicy createPolicy(String actor, AuthPolicy.ActorType actorType,
                                    String audience, String tenantId, String permissions) {
        var policy = new AuthPolicy(actor, actorType, audience, tenantId, permissions);
        return policyRepo.save(policy);
    }

    // ── Brute-force protection ───────────────────────────────

    private void checkRateLimit(String clientId) {
        FailedAttempt attempt = failedAttempts.get(clientId);
        if (attempt != null && attempt.count >= tokenProperties.getMaxFailedAttempts()) {
            long elapsed = System.currentTimeMillis() - attempt.lastAttempt;
            if (elapsed < tokenProperties.getLockoutDurationMs()) {
                long remaining = (tokenProperties.getLockoutDurationMs() - elapsed) / 1000;
                log.warn("Client '{}' locked out for {}s after {} failed attempts",
                        clientId, remaining, attempt.count);
                throw new AuthException("Too many failed attempts. Try again in " + remaining + "s");
            }
            failedAttempts.remove(clientId); // lockout expired
        }
    }

    private void recordFailedAttempt(String clientId) {
        failedAttempts.compute(clientId, (k, existing) -> {
            if (existing == null) existing = new FailedAttempt();
            existing.count++;
            existing.lastAttempt = System.currentTimeMillis();
            return existing;
        });
    }

    private static class FailedAttempt {
        int count;
        long lastAttempt;
    }
}
