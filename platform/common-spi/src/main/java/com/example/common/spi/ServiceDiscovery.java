package com.example.common.spi;

/**
 * Service Discovery SPI — pluggable service resolution.
 *
 * Implementations:
 * - StaticDiscovery: environment/config-based static map (dev/test)
 * - NacosDiscovery: real Nacos HTTP API with registration, heartbeat, cache
 *
 * Selection: DiscoveryProvider.load() reads DISCOVERY_MODE env var.
 */
public interface ServiceDiscovery {

    /**
     * Resolve a service name to its network address.
     * @param serviceName logical service name (e.g., "memory-server")
     * @return address in format "http://host:port", or null if not found
     */
    String resolve(String serviceName);
}
