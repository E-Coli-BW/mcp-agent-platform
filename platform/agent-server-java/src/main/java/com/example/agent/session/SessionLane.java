package com.example.agent.session;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

import java.time.Duration;

/**
 * Redis-backed session lock with graceful degradation.
 */
@Component
public class SessionLane {

    private static final Logger log = LoggerFactory.getLogger(SessionLane.class);
    private static final Duration DEFAULT_LOCK_TTL = Duration.ofSeconds(120);

    private final ReactiveRedisTemplate<String, String> redisTemplate;

    public SessionLane(ReactiveRedisTemplate<String, String> redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    /**
     * Try to acquire the lock.
     *
     * @param sessionId session identifier
     * @param timeout lock expiry
     * @return true if acquired or Redis is unavailable
     */
    public Mono<Boolean> acquireLock(String sessionId, Duration timeout) {
        return redisTemplate.opsForValue()
                .setIfAbsent(key(sessionId), "1", timeout)
                .defaultIfEmpty(false)
                .onErrorResume(e -> {
                    log.debug("Session lock unavailable (Redis): {}", e.getMessage());
                    return Mono.just(true);
                });
    }

    /**
     * Release the lock.
     *
     * @param sessionId session identifier
     * @return completion signal
     */
    public Mono<Void> releaseLock(String sessionId) {
        return redisTemplate.delete(key(sessionId))
                .then()
                .onErrorResume(e -> {
                    log.debug("Session lock release failed (Redis): {}", e.getMessage());
                    return Mono.empty();
                });
    }

    /**
     * Poll until the lock is acquired or timeout is reached.
     *
     * @param sessionId session identifier
     * @param maxWait maximum wait duration
     * @return true when the lock is acquired
     */
    public Mono<Boolean> waitForLock(String sessionId, Duration maxWait) {
        long attempts = Math.max(1L, (maxWait.toMillis() + 499L) / 500L);
        return Flux.interval(Duration.ZERO, Duration.ofMillis(500))
                .take(attempts)
                .concatMap(ignored -> acquireLock(sessionId, DEFAULT_LOCK_TTL))
                .filter(Boolean.TRUE::equals)
                .next()
                .defaultIfEmpty(false)
                .onErrorResume(e -> {
                    log.debug("Session wait failed (Redis): {}", e.getMessage());
                    return Mono.just(true);
                });
    }

    private String key(String sessionId) {
        return "session_lane:" + sessionId;
    }
}
