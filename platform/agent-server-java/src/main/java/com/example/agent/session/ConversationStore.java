package com.example.agent.session;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;

/**
 * Redis-backed conversation store with graceful degradation.
 */
@Component
public class ConversationStore {

    private static final Logger log = LoggerFactory.getLogger(ConversationStore.class);
    private static final int MAX_MESSAGES = 20;
    private static final Duration TTL = Duration.ofMinutes(30);

    private final ReactiveRedisTemplate<String, String> redisTemplate;
    private final ObjectMapper mapper;

    public ConversationStore(ReactiveRedisTemplate<String, String> redisTemplate, ObjectMapper mapper) {
        this.redisTemplate = redisTemplate;
        this.mapper = mapper;
    }

    /**
     * Append a message to Redis.
     *
     * @param sessionId session identifier
     * @param message message to append
     * @return completion signal
     */
    public Mono<Void> append(String sessionId, Message message) {
        String payload;
        try {
            payload = mapper.writeValueAsString(message);
        }
        catch (JsonProcessingException e) {
            log.debug("Conversation serialization failed: {}", e.getMessage());
            return Mono.empty();
        }

        String key = key(sessionId);
        return redisTemplate.opsForList().rightPush(key, payload)
                .then(redisTemplate.opsForList().trim(key, -MAX_MESSAGES, -1))
                .then(redisTemplate.expire(key, TTL))
                .then()
                .onErrorResume(e -> {
                    log.debug("Conversation append failed (Redis): {}", e.getMessage());
                    return Mono.empty();
                });
    }

    /**
     * Read messages from Redis.
     *
     * @param sessionId session identifier
     * @return message stream
     */
    public Flux<Message> getMessages(String sessionId) {
        return redisTemplate.opsForList().range(key(sessionId), 0, -1)
                .flatMap(this::deserialize)
                .onErrorResume(e -> {
                    log.debug("Conversation read failed (Redis): {}", e.getMessage());
                    return Flux.empty();
                });
    }

    private Mono<Message> deserialize(String payload) {
        try {
            return Mono.just(mapper.readValue(payload, Message.class));
        }
        catch (Exception e) {
            log.debug("Conversation deserialization failed: {}", e.getMessage());
            return Mono.empty();
        }
    }

    private String key(String sessionId) {
        return "conv:" + sessionId;
    }
}
