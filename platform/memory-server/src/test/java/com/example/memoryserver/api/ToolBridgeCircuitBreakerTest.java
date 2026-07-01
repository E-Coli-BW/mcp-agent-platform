package com.example.memoryserver.api;

import com.example.memoryserver.model.MemoryEntity;
import com.example.memoryserver.search.MemorySearchEngine.ScoredResult;
import com.example.memoryserver.service.MemoryService;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Date;
import java.util.List;

import static org.hamcrest.Matchers.containsString;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest(properties = "mcp.security.jwt-secret=test-secret-at-least-32-bytes-long!!")
@ActiveProfiles("integration-test")
@AutoConfigureMockMvc
@TestPropertySource(properties = {
        "resilience4j.circuitbreaker.instances.memoryService.failureRateThreshold=50",
        "resilience4j.circuitbreaker.instances.memoryService.slidingWindowSize=4",
        "resilience4j.circuitbreaker.instances.memoryService.minimumNumberOfCalls=4",
        "resilience4j.circuitbreaker.instances.memoryService.waitDurationInOpenState=10s"
})
class ToolBridgeCircuitBreakerTest {

    private static final String JWT_SECRET = "test-secret-at-least-32-bytes-long!!";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private CircuitBreakerRegistry circuitBreakerRegistry;

    @MockBean
    private MemoryService memoryService;

    private CircuitBreaker circuitBreaker;

    @BeforeEach
    void setUp() {
        reset(memoryService);
        circuitBreaker = circuitBreakerRegistry.circuitBreaker("memoryService");
        circuitBreaker.reset();
    }

    @AfterEach
    void tearDown() {
        circuitBreaker.reset();
    }

    @Test
    void should_returnFiveHundred_when_serviceThrows() throws Exception {
        when(memoryService.search(anyString(), anyString(), isNull(), isNull(), anyInt()))
                .thenThrow(new RuntimeException("DB down"));

        mockMvc.perform(authorizedPost("/api/tools/memory_search", "{\"query\":\"failure\"}"))
                .andExpect(status().isInternalServerError())
                .andExpect(jsonPath("$.result").value(containsString("❌ Service error:")));
    }

    @Test
    void should_openBreaker_after_thresholdFailures() throws Exception {
        when(memoryService.search(anyString(), anyString(), isNull(), isNull(), anyInt()))
                .thenThrow(new RuntimeException("DB down"));

        for (int i = 0; i < 4; i++) {
            mockMvc.perform(authorizedPost("/api/tools/memory_search", "{\"query\":\"failure\"}"))
                    .andExpect(status().isInternalServerError());
        }

        assertEquals(CircuitBreaker.State.OPEN, circuitBreaker.getState());

        mockMvc.perform(authorizedPost("/api/tools/memory_search", "{\"query\":\"failure\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.result").value(containsString("❌ Service temporarily unavailable (circuit breaker open)")));
    }

    @Test
    void should_recordSuccess_when_serviceReturnsNormally() throws Exception {
        MemoryEntity entity = new MemoryEntity("tenant-a", "k1", "useful content", "default");
        when(memoryService.search(anyString(), anyString(), isNull(), isNull(), anyInt()))
                .thenReturn(List.of(new ScoredResult(entity, 0.99)));

        mockMvc.perform(authorizedPost("/api/tools/memory_search", "{\"query\":\"useful\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.result").value(containsString("🔍 Found 1 result(s) for \"useful\"")));

        assertEquals(0, circuitBreaker.getMetrics().getNumberOfFailedCalls());
    }

    @Test
    void should_keepValidationErrors_outOfBreakerCount_when_keyMissing() throws Exception {
        long failedBefore = circuitBreaker.getMetrics().getNumberOfFailedCalls();

        mockMvc.perform(authorizedPost("/api/tools/memory_set", "{\"content\":\"hello\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.result").value("❌ key is required"));

        assertEquals(failedBefore, circuitBreaker.getMetrics().getNumberOfFailedCalls());
    }

    @Test
    void should_includeOriginalCauseMessage_when_serviceThrows() throws Exception {
        when(memoryService.search(anyString(), anyString(), isNull(), isNull(), anyInt()))
                .thenThrow(new RuntimeException("DB down"));

        MvcResult result = mockMvc.perform(authorizedPost("/api/tools/memory_search", "{\"query\":\"failure\"}"))
                .andExpect(status().isInternalServerError())
                .andExpect(content().string(containsString("DB down")))
                .andReturn();

        assertTrue(result.getResponse().getContentAsString().contains("DB down"));
    }

    private org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder authorizedPost(String path, String body) {
        return post(path)
                .header("Authorization", "Bearer " + validJwt("tenant-a"))
                .contentType(MediaType.APPLICATION_JSON)
                .content(body);
    }

    private String validJwt(String tenantId) {
        byte[] keyBytes = JWT_SECRET.getBytes(StandardCharsets.UTF_8);
        SecretKey key = Keys.hmacShaKeyFor(keyBytes);
        return Jwts.builder()
                .subject("test-service")
                .claim("tenant_id", tenantId)
                .issuedAt(Date.from(Instant.now()))
                .expiration(Date.from(Instant.now().plus(1, ChronoUnit.HOURS)))
                .signWith(key)
                .compact();
    }
}
