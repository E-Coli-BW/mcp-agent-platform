package com.example.common.spi;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for NacosDiscovery JSON parsing and cache behavior.
 * These tests don't require a running Nacos server.
 */
class NacosDiscoveryTest {

    private final NacosDiscovery nacos = new NacosDiscovery("http://localhost:8848");

    // ─── JSON Parsing Tests ────────────────────────────────────────────

    @Test
    void should_parseHealthyHost_when_validNacosResponse() {
        String json = """
            {
              "hosts": [
                {"ip": "192.168.1.10", "port": 8180, "healthy": true, "weight": 1.0}
              ],
              "dom": "memory-server",
              "cacheMillis": 10000
            }
            """;
        String result = nacos.parseFirstHealthyHost(json);
        assertEquals("http://192.168.1.10:8180", result);
    }

    @Test
    void should_parseFirstHost_when_multipleInstances() {
        String json = """
            {
              "hosts": [
                {"ip": "10.0.0.1", "port": 8180, "healthy": true},
                {"ip": "10.0.0.2", "port": 8180, "healthy": true}
              ]
            }
            """;
        // Should return the first healthy instance
        String result = nacos.parseFirstHealthyHost(json);
        assertEquals("http://10.0.0.1:8180", result);
    }

    @Test
    void should_returnNull_when_emptyHostsList() {
        String json = """
            {
              "hosts": [],
              "dom": "memory-server"
            }
            """;
        String result = nacos.parseFirstHealthyHost(json);
        assertNull(result);
    }

    @Test
    void should_returnNull_when_noHostsKey() {
        String json = """
            {"error": "service not found"}
            """;
        String result = nacos.parseFirstHealthyHost(json);
        assertNull(result);
    }

    @Test
    void should_returnNull_when_malformedJson() {
        String result = nacos.parseFirstHealthyHost("not json at all");
        assertNull(result);
    }

    @Test
    void should_parseHost_when_ipIsLocalhost() {
        String json = """
            {"hosts":[{"ip":"127.0.0.1","port":8380,"healthy":true}]}
            """;
        String result = nacos.parseFirstHealthyHost(json);
        assertEquals("http://127.0.0.1:8380", result);
    }

    // ─── Cache Behavior Tests ──────────────────────────────────────────

    @Test
    void should_returnNull_when_nacosUnreachableAndNoCachedValue() {
        // Nacos at fake URL → unreachable, no cache → returns null
        NacosDiscovery unreachable = new NacosDiscovery("http://localhost:1");
        String result = unreachable.resolve("non-existent-service");
        assertNull(result);
        unreachable.shutdown();
    }

    @Test
    void should_shutdownGracefully_when_calledMultipleTimes() {
        NacosDiscovery instance = new NacosDiscovery("http://localhost:8848");
        assertDoesNotThrow(instance::shutdown);
        assertDoesNotThrow(instance::shutdown); // idempotent
    }
}
