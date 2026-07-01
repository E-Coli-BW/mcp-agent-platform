package com.example.memorystore.config;

import com.example.memorystore.MemoryStoreService;
import com.example.memorystore.spi.MemoryStorageBackend;
import com.example.memorystore.spi.ServiceDiscovery;
import com.example.memorystore.spi.bridge.SpringElasticsearchOperations;
import com.example.memorystore.spi.bridge.SpringNacosOperations;
import com.example.memorystore.spi.bridge.SpringRedisOperations;
import com.example.memorystore.spi.impl.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.core.StringRedisTemplate;

import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;

/**
 * Auto-configures MemoryStoreService based on properties:
 *
 *   memorystore.backend = file | redis | elasticsearch
 *   memorystore.discovery = static | nacos
 *
 * Defaults to file + static for dev; override for production.
 */
@Configuration
public class MemoryStoreAutoConfig {

    private static final Logger log = LoggerFactory.getLogger(MemoryStoreAutoConfig.class);

    // ── Storage Backend ──────────────────────────────────────

    @Bean
    @ConditionalOnProperty(name = "memorystore.backend", havingValue = "file", matchIfMissing = true)
    public MemoryStorageBackend fileBackend(
            @Value("${memorystore.file.base-dir:#{systemProperties['user.home'] + '/.mcp-local/memory-store'}}") String baseDir) {
        log.info("📁 MemoryStore backend: FILE ({})", baseDir);
        return new FileMemoryBackend(Paths.get(baseDir));
    }

    @Bean
    @ConditionalOnProperty(name = "memorystore.backend", havingValue = "redis")
    public MemoryStorageBackend redisBackend(StringRedisTemplate redisTemplate) {
        log.info("🔴 MemoryStore backend: REDIS");
        return new RedisMemoryBackend(new SpringRedisOperations(redisTemplate));
    }

    @Bean
    @ConditionalOnProperty(name = "memorystore.backend", havingValue = "elasticsearch")
    public MemoryStorageBackend esBackend(co.elastic.clients.elasticsearch.ElasticsearchClient esClient) {
        log.info("🔍 MemoryStore backend: ELASTICSEARCH");
        return new ElasticsearchMemoryBackend(new SpringElasticsearchOperations(esClient));
    }

    // ── Service Discovery ────────────────────────────────────

    @Bean
    @ConditionalOnProperty(name = "memorystore.discovery", havingValue = "static", matchIfMissing = true)
    public ServiceDiscovery staticDiscovery(
            @Value("${memorystore.static.nodes:localhost:8180}") String nodes) {
        List<String> nodeList = Arrays.asList(nodes.split(","));
        log.info("📌 MemoryStore discovery: STATIC ({})", nodeList);
        return new StaticServiceDiscovery(nodeList);
    }

    @Bean
    @ConditionalOnProperty(name = "memorystore.discovery", havingValue = "nacos")
    public ServiceDiscovery nacosDiscovery(
            @Value("${memorystore.nacos.server-addr:localhost:8848}") String serverAddr) {
        log.info("☁️ MemoryStore discovery: NACOS ({})", serverAddr);
        try {
            var namingService = com.alibaba.nacos.api.NacosFactory.createNamingService(serverAddr);
            return new NacosServiceDiscovery(new SpringNacosOperations(namingService));
        } catch (Exception e) {
            log.error("Failed to connect to Nacos at {}, falling back to static", serverAddr, e);
            return new StaticServiceDiscovery(List.of("localhost:8180"));
        }
    }

    // ── Service ──────────────────────────────────────────────

    @Bean
    public MemoryStoreService memoryStoreService(MemoryStorageBackend backend, ServiceDiscovery discovery) {
        log.info("✅ MemoryStoreService wired: backend={}, discovery={}",
                backend.getClass().getSimpleName(), discovery.getClass().getSimpleName());
        return new MemoryStoreService(backend, discovery);
    }
}
