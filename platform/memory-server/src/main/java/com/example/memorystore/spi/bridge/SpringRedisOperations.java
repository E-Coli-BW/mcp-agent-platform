package com.example.memorystore.spi.bridge;

import com.example.memorystore.spi.impl.RedisMemoryBackend;
import org.springframework.data.redis.core.StringRedisTemplate;

import java.util.*;

/**
 * Bridges Spring Boot's StringRedisTemplate to our RedisOperations SPI.
 * Use this in production Spring context to wire RedisMemoryBackend.
 */
public class SpringRedisOperations implements RedisMemoryBackend.RedisOperations {

    private final StringRedisTemplate redis;

    public SpringRedisOperations(StringRedisTemplate redis) {
        this.redis = redis;
    }

    @Override
    public void hset(String hashKey, String field, String value) {
        redis.opsForHash().put(hashKey, field, value);
    }

    @Override
    public String hget(String hashKey, String field) {
        Object val = redis.opsForHash().get(hashKey, field);
        return val == null ? null : val.toString();
    }

    @Override
    public boolean hdel(String hashKey, String field) {
        Long removed = redis.opsForHash().delete(hashKey, field);
        return removed != null && removed > 0;
    }

    @Override
    public Map<String, String> hgetAll(String hashKey) {
        Map<Object, Object> raw = redis.opsForHash().entries(hashKey);
        Map<String, String> result = new HashMap<>();
        raw.forEach((k, v) -> result.put(k.toString(), v.toString()));
        return result;
    }

    @Override
    public Set<String> hkeys(String hashKey) {
        Set<Object> raw = redis.opsForHash().keys(hashKey);
        Set<String> result = new HashSet<>();
        raw.forEach(k -> result.add(k.toString()));
        return result;
    }
}
