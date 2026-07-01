package com.example.common.spi;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class DiscoveryProviderTest {
    @Test
    void should_loadStaticDiscovery_when_modeIsNull() {
        ServiceDiscovery discovery = DiscoveryProvider.load(null);
        assertTrue(discovery instanceof StaticDiscovery);
        assertEquals("http://localhost:8180", discovery.resolve("memory-server"));
    }

    @Test
    void should_loadStaticDiscovery_when_modeIsEmpty() {
        ServiceDiscovery discovery = DiscoveryProvider.load("");
        assertTrue(discovery instanceof StaticDiscovery);
    }

    @Test
    void should_loadNacosDiscovery_when_modeIsNacos() {
        ServiceDiscovery discovery = DiscoveryProvider.load("nacos");
        assertTrue(discovery instanceof NacosDiscovery);
    }

    @Test
    void should_resolveAllStaticServices_when_usingStaticMode() {
        ServiceDiscovery discovery = DiscoveryProvider.load("static");
        assertEquals("http://localhost:8180", discovery.resolve("memory-server"));
        assertEquals("http://localhost:8280", discovery.resolve("filesearch-server"));
        assertEquals("http://localhost:8380", discovery.resolve("codeexec-server"));
        assertEquals("http://localhost:8090", discovery.resolve("auth-service"));
        assertEquals("http://localhost:8480", discovery.resolve("model-router"));
    }

    @Test
    void should_returnNull_when_staticServiceNotFound() {
        ServiceDiscovery discovery = DiscoveryProvider.load(null);
        assertNull(discovery.resolve("unknown-service"));
    }
}
