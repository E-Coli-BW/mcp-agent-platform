package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.ServiceDiscovery;

import java.util.List;

/**
 * Nacos-based service discovery.
 *
 * Abstracts the Nacos NamingService API behind our SPI interface.
 * In production, inject a real NamingService; for tests, use the stub constructor.
 */
public class NacosServiceDiscovery implements ServiceDiscovery {

    private final NacosOperations ops;

    /**
     * Abstraction over Nacos NamingService — allows real Nacos or a test stub.
     */
    public interface NacosOperations {
        List<String> getInstances(String serviceName);
    }

    public NacosServiceDiscovery(NacosOperations ops) {
        this.ops = ops;
    }

    @Override
    public List<String> getAvailableNodes(String serviceName) {
        return ops.getInstances(serviceName);
    }
}
