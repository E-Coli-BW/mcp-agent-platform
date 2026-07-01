package com.example.auth.service;

import com.example.auth.model.AuthUser;
import com.example.auth.model.OutboxEvent;
import com.example.auth.repository.AuthUserRepository;
import com.example.auth.repository.OutboxEventRepository;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.io.IOException;
import java.time.Instant;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

@SpringBootTest(properties = "spring.datasource.url=jdbc:h2:mem:auth-service-outbox-test;DB_CLOSE_DELAY=-1")
class OutboxTest {

    @Autowired
    private UserService userService;

    @Autowired
    private AuthUserRepository userRepo;

    @Autowired
    private OutboxEventRepository outboxRepo;

    private final ObjectMapper objectMapper = new ObjectMapper();

    @BeforeEach
    void setup() {
        outboxRepo.deleteAll();
        userRepo.deleteAll();
    }

    @Test
    void should_createOutboxEvent_when_signupSucceeds() {
        userService.signup("olivia", "password123", "olivia@test.com", "tenant-1");

        List<OutboxEvent> events = outboxRepo.findAll();
        assertEquals(1, events.size());

        OutboxEvent event = events.getFirst();
        assertEquals("user.events", event.getTopic());
        assertEquals("tenant-1", event.getEventKey());
        assertFalse(event.isPublished());
        assertTrue(event.getPayload().contains("USER_REGISTERED"));
    }

    @Test
    void should_haveOutboxEventInSameTransaction_when_signupSucceeds() {
        var result = userService.signup("peter", "password123", "peter@test.com", "tenant-2");

        AuthUser user = userRepo.findByUsername("peter").orElseThrow();
        OutboxEvent event = outboxRepo.findAll().stream().findFirst().orElseThrow();

        assertEquals(result.userId(), user.getId());
        assertEquals("tenant-2", event.getEventKey());
        assertTrue(event.getPayload().contains("\"userId\":" + user.getId()));
    }

    @Test
    void should_containCorrectPayload_when_outboxEventCreated() throws IOException {
        var result = userService.signup("quinn", "password123", "quinn@test.com", "tenant-3");

        OutboxEvent event = outboxRepo.findAll().stream().findFirst().orElseThrow();
        JsonNode payload = objectMapper.readTree(event.getPayload());

        assertEquals("USER_REGISTERED", payload.get("type").asText());
        assertEquals(result.userId(), payload.get("userId").asLong());
        assertEquals("quinn", payload.get("username").asText());
        assertEquals("quinn@test.com", payload.get("email").asText());
        assertEquals("tenant-3", payload.get("tenantId").asText());
        assertDoesNotThrow(() -> Instant.parse(payload.get("timestamp").asText()));
    }
}
