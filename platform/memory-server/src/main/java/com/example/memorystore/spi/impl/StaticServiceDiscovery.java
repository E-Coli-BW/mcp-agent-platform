package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.ServiceDiscovery;
import java.util.List;

public class StaticServiceDiscovery implements ServiceDiscovery {
    private final List<String> nodes;

    public StaticServiceDiscovery(List<String> nodes) {
        this.nodes = nodes;
    }

    @Override
    public List<String> getAvailableNodes(String serviceName) {
        return nodes;
    }
}
