package com.example.auth;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.hamcrest.Matchers.*;

/**
 * Auth Service integration tests.
 * Tests the full flow: register → authenticate → get JWKS → verify token.
 */
@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-auth-test;DB_CLOSE_DELAY=-1")
@AutoConfigureMockMvc
class AuthServiceTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private com.example.auth.security.RsaKeyManager keyManager;

    /** Generate an admin JWT for /register tests. */
    private String adminJwt() {
        return io.jsonwebtoken.Jwts.builder()
                .subject("test-admin")
                .claim("tenant_id", "admin-tenant")
                .claim("roles", java.util.List.of("ADMIN"))
                .issuedAt(java.util.Date.from(java.time.Instant.now()))
                .expiration(java.util.Date.from(java.time.Instant.now().plusSeconds(3600)))
                .signWith(keyManager.getPrivateKey())
                .compact();
    }

    // ── JWKS ─────────────────────────────────────────────────

    @Test
    void jwks_returnsPublicKey() throws Exception {
        mockMvc.perform(get("/auth/jwks"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.keys").isArray())
                .andExpect(jsonPath("$.keys[0].kty").value("RSA"))
                .andExpect(jsonPath("$.keys[0].alg").value("RS256"))
                .andExpect(jsonPath("$.keys[0].kid").isString())
                .andExpect(jsonPath("$.keys[0].n").isString())
                .andExpect(jsonPath("$.keys[0].e").isString());
    }

    // ── Token Endpoint ───────────────────────────────────────

    @Test
    void token_validCredentials_returnsJwt() throws Exception {
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"agent-server",
                             "client_secret":"agent-secret","audience":"memory-server"}
                            """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.access_token").isString())
                .andExpect(jsonPath("$.token_type").value("Bearer"))
                .andExpect(jsonPath("$.expires_in").value(3600));
    }

    @Test
    void token_invalidSecret_returns401() throws Exception {
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"agent-server","client_secret":"wrong-secret"}
                            """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.error").value("invalid_client"));
    }

    @Test
    void token_unknownClient_returns401() throws Exception {
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"nonexistent","client_secret":"whatever"}
                            """))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void token_wrongGrantType_returns400() throws Exception {
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"authorization_code","client_id":"agent-server","client_secret":"agent-secret"}
                            """))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").value("invalid_request"));
    }

    // ── Client Registration ──────────────────────────────────

    @Test
    void register_newClient_succeeds() throws Exception {
        mockMvc.perform(post("/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .header("Authorization", "Bearer " + adminJwt())
                        .content("{\"client_id\":\"new-service\",\"client_secret\":\"new-secret\",\"scopes\":\"data:read\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.client_id").value("new-service"));

        // Verify the new client can get a token
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"new-service","client_secret":"new-secret"}
                            """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.access_token").isString());
    }

    @Test
    void register_duplicateClient_returns409() throws Exception {
        // "agent-server" is already registered by DefaultClientInitializer
        mockMvc.perform(post("/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .header("Authorization", "Bearer " + adminJwt())
                        .content("{\"client_id\":\"agent-server\",\"client_secret\":\"whatever\"}"))
                .andExpect(status().isConflict());
    }

    // ── Health ────────────────────────────────────────────────

    @Test
    void health_returnsUp() throws Exception {
        mockMvc.perform(get("/auth/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("UP"));
    }

    // ── Token Content Verification ───────────────────────────

    @Test
    void token_containsCorrectClaims() throws Exception {
        var result = mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"agent-server",
                             "client_secret":"agent-secret","audience":"memory-server"}
                            """))
                .andExpect(status().isOk())
                .andReturn();

        String token = com.fasterxml.jackson.databind.ObjectMapper.class
                .getDeclaredConstructor().newInstance()
                .readTree(result.getResponse().getContentAsString())
                .get("access_token").asText();

        // Decode JWT payload (no verification — just parse claims)
        String[] parts = token.split("\\.");
        String payload = new String(java.util.Base64.getUrlDecoder().decode(parts[1]));
        var claims = new com.fasterxml.jackson.databind.ObjectMapper().readTree(payload);

        // Verify claims
        assert claims.get("iss").asText().equals("mcp-auth-service");
        assert claims.get("sub").asText().equals("agent-server");
        assert claims.get("tenant_id").asText().equals("default");
        assert claims.has("permissions") : "Token must contain 'permissions' claim";
        assert claims.has("sub_type") : "Token must contain 'sub_type' claim";
        assert claims.get("sub_type").asText().equals("SERVICE");
        assert claims.has("exp");
        assert claims.has("jti");

        // Verify policy-derived permissions (from seeded policy: agent-server → memory-server)
        var perms = claims.get("permissions");
        assert perms.isArray() : "permissions must be an array";
        boolean hasMemRead = false;
        for (var p : perms) {
            if ("MEMORY_READ".equals(p.asText())) hasMemRead = true;
        }
        assert hasMemRead : "Expected MEMORY_READ in permissions from policy";
    }

    // ── Policy-Based Authorization ───────────────────────────

    @Test
    void token_policyRestricts_audience() throws Exception {
        mockMvc.perform(post("/auth/token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                            {"grant_type":"client_credentials","client_id":"web-frontend",
                             "client_secret":"web-secret","audience":"memory-server"}
                            """))
                .andExpect(status().isOk()); // fallback to client scopes
    }
}
