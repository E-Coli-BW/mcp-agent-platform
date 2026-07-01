package com.example.auth.service;

import com.example.auth.model.AuthPolicy;
import com.example.auth.repository.AuthPolicyRepository;
import com.example.auth.security.RsaKeyManager;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-token-privilege-test;DB_CLOSE_DELAY=-1")
@ActiveProfiles("test")
class TokenServicePrivilegeTest {

    @Autowired
    private TokenService tokenService;

    @Autowired
    private AuthPolicyRepository policyRepository;

    @Autowired
    private RsaKeyManager keyManager;

    @Test
    void should_rejectCrossTenant_when_clientHasNoPolicy() {
        TestClient client = registerTenantClient("tenant-A");

        TokenService.AuthException exception = assertThrows(TokenService.AuthException.class,
                () -> tokenService.issueToken(client.clientId(), client.clientSecret(), "memory-server", "tenant-B"));

        assertEquals("Client not authorized to mint tokens for tenant 'tenant-B'", exception.getMessage());
    }

    @Test
    void should_allowCrossTenant_when_policyGrantsMintCrossTenant() {
        List<AuthPolicy> policies = policyRepository.findMatchingPolicies("admin-cli", "auth-service", "tenant-B");

        assertFalse(policies.isEmpty());
        assertTrue(policies.stream()
                .flatMap(policy -> policy.getPermissionList().stream())
                .anyMatch(permission -> permission.equals("MINT_CROSS_TENANT") || permission.equals("*")));

        TokenService.TokenResponse response = tokenService.issueToken("admin-cli", "admin-secret", "memory-server", "tenant-B");
        Claims claims = parseClaims(response.accessToken());

        assertEquals("tenant-B", claims.get("tenant_id", String.class));
    }

    @Test
    void should_allowSameTenant_byDefault() {
        TestClient client = registerTenantClient("tenant-A");

        TokenService.TokenResponse response = tokenService.issueToken(client.clientId(), client.clientSecret(), "memory-server", "tenant-A");
        Claims claims = parseClaims(response.accessToken());

        assertEquals("tenant-A", claims.get("tenant_id", String.class));
    }

    @Test
    void should_allowOmittedTenant_thenFallsBackToClientTenant() {
        TestClient client = registerTenantClient("tenant-A");

        TokenService.TokenResponse response = tokenService.issueToken(client.clientId(), client.clientSecret(), "memory-server", null);
        Claims claims = parseClaims(response.accessToken());

        assertEquals("tenant-A", claims.get("tenant_id", String.class));
    }

    @Test
    void should_runPolicyCheck_afterSecretVerification() {
        TestClient client = registerTenantClient("tenant-A");

        TokenService.AuthException exception = assertThrows(TokenService.AuthException.class,
                () -> tokenService.issueToken(client.clientId(), "wrong-secret", "memory-server", "tenant-B"));

        assertEquals("Invalid client_secret", exception.getMessage());
    }

    private TestClient registerTenantClient(String tenantId) {
        String suffix = UUID.randomUUID().toString();
        String clientId = "tenant-client-" + suffix;
        String clientSecret = "secret-" + suffix;
        tokenService.registerClient(clientId, clientSecret, "Tenant Client", "chat:*", tenantId);
        return new TestClient(clientId, clientSecret, tenantId);
    }

    private Claims parseClaims(String token) {
        return Jwts.parser()
                .verifyWith(keyManager.getPublicKey())
                .build()
                .parseSignedClaims(token)
                .getPayload();
    }

    private record TestClient(String clientId, String clientSecret, String tenantId) {
    }
}
