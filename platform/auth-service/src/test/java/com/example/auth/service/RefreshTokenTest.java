package com.example.auth.service;

import com.example.auth.model.RefreshToken;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.repository.RefreshTokenRepository;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.temporal.ChronoUnit;

import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-refresh-token-test;DB_CLOSE_DELAY=-1")
@AutoConfigureMockMvc
class RefreshTokenTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Autowired
    private UserService userService;

    @Autowired
    private AuthUserRepository userRepo;

    @Autowired
    private RefreshTokenRepository refreshTokenRepo;

    @BeforeEach
    void setup() {
        refreshTokenRepo.deleteAll();
        userRepo.deleteAll();
    }

    @Test
    void should_returnRefreshToken_when_loginSuccess() throws Exception {
        userService.signup("alice-refresh", "password123", "alice@test.com", "tenant-1");

        MvcResult result = mockMvc.perform(post("/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"alice-refresh\",\"password\":\"password123\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.access_token").isString())
                .andExpect(jsonPath("$.token_type").value("Bearer"))
                .andExpect(jsonPath("$.expires_in").value(3600))
                .andExpect(jsonPath("$.tenant_id").value("tenant-1"))
                .andExpect(jsonPath("$.refresh_token").isString())
                .andReturn();

        JsonNode json = objectMapper.readTree(result.getResponse().getContentAsString());
        String refreshToken = json.get("refresh_token").asText();
        var user = userRepo.findByUsername("alice-refresh").orElseThrow();

        assertFalse(refreshToken.isBlank());
        assertEquals(1, refreshTokenRepo.findByUserIdAndRevokedFalse(user.getId()).size());
        assertTrue(refreshTokenRepo.findByTokenHashAndRevokedFalse(sha256(refreshToken)).isPresent());
    }

    @Test
    void should_issueNewTokenPair_when_refreshTokenValid() throws Exception {
        userService.signup("bob-refresh", "password123", null, "tenant-2");
        var login = userService.login("bob-refresh", "password123");
        var user = userRepo.findByUsername("bob-refresh").orElseThrow();

        MvcResult result = mockMvc.perform(post("/auth/refresh")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"refresh_token\":\"" + login.refreshToken() + "\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.access_token").isString())
                .andExpect(jsonPath("$.token_type").value("Bearer"))
                .andExpect(jsonPath("$.expires_in").value(3600))
                .andExpect(jsonPath("$.refresh_token").isString())
                .andReturn();

        JsonNode json = objectMapper.readTree(result.getResponse().getContentAsString());
        String newRefreshToken = json.get("refresh_token").asText();

        assertNotEquals(login.refreshToken(), newRefreshToken);
        assertTrue(refreshTokenRepo.findByTokenHashAndRevokedFalse(sha256(login.refreshToken())).isEmpty());
        assertTrue(refreshTokenRepo.findByTokenHashAndRevokedFalse(sha256(newRefreshToken)).isPresent());
        assertEquals(1, refreshTokenRepo.findByUserIdAndRevokedFalse(user.getId()).size());
    }

    @Test
    void should_reject_when_refreshTokenRevoked() throws Exception {
        userService.signup("carol-refresh", "password123", null, "tenant-3");
        var login = userService.login("carol-refresh", "password123");

        mockMvc.perform(post("/auth/refresh")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"refresh_token\":\"" + login.refreshToken() + "\"}"))
                .andExpect(status().isOk());

        mockMvc.perform(post("/auth/refresh")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"refresh_token\":\"" + login.refreshToken() + "\"}"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.error").value("invalid_token"));
    }

    @Test
    void should_reject_when_refreshTokenExpired() throws Exception {
        userService.signup("dave-refresh", "password123", null, "tenant-4");
        var login = userService.login("dave-refresh", "password123");

        RefreshToken stored = refreshTokenRepo.findByTokenHashAndRevokedFalse(sha256(login.refreshToken())).orElseThrow();
        stored.setExpiresAt(Instant.now().minus(1, ChronoUnit.DAYS));
        refreshTokenRepo.save(stored);

        mockMvc.perform(post("/auth/refresh")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"refresh_token\":\"" + login.refreshToken() + "\"}"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.error").value("invalid_token"));

        assertTrue(refreshTokenRepo.findByTokenHashAndRevokedFalse(sha256(login.refreshToken())).isEmpty());
    }

    @Test
    void should_revokeAllTokens_when_logout() throws Exception {
        userService.signup("erin-refresh", "password123", null, "tenant-5");
        var loginResult = userService.login("erin-refresh", "password123");
        userService.login("erin-refresh", "password123");
        var user = userRepo.findByUsername("erin-refresh").orElseThrow();

        assertEquals(2, refreshTokenRepo.findByUserIdAndRevokedFalse(user.getId()).size());

        // Logout using the access token in Authorization header
        mockMvc.perform(post("/auth/logout")
                        .header("Authorization", "Bearer " + loginResult.accessToken()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.message").value("Logged out successfully"));

        assertTrue(refreshTokenRepo.findByUserIdAndRevokedFalse(user.getId()).isEmpty());
    }

    private String sha256(String input) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest(input.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : hash) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
