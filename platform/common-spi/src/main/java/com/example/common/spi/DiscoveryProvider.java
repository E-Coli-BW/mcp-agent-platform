package com.example.common.spi;

import java.util.Map;

/**
 * Factory for ServiceDiscovery implementations.
 *
 * Reads DISCOVERY_MODE env var:
 *   - "nacos" → NacosDiscovery (connects to NACOS_URL, default localhost:8848)
 *   - anything else → StaticDiscovery with hardcoded dev endpoints
 *
 * Usage:
 *   ServiceDiscovery discovery = DiscoveryProvider.load();
 *   String url = discovery.resolve("memory-server");
 */
public class DiscoveryProvider {

    private DiscoveryProvider() {}

    // For production use (reads env vars)
    public static ServiceDiscovery load() {
        String mode = System.getenv("DISCOVERY_MODE");
        return load(mode);
    }

    // For testability (explicit mode)
    public static ServiceDiscovery load(String mode) {
        if ("nacos".equalsIgnoreCase(mode)) {
            String nacosUrl = System.getenv().getOrDefault("NACOS_URL", "http://localhost:8848");
            return new NacosDiscovery(nacosUrl);
        } else {
            // Fallback: static config for local development
            return new StaticDiscovery(Map.of(
                "memory-server", "http://localhost:8180",
                "filesearch-server", "http://localhost:8280",
                "codeexec-server", "http://localhost:8380",
                "auth-service", "http://localhost:8090",
                "model-router", "http://localhost:8480"
            ));
        }
    }
}
