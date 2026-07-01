package com.example.memorystore.spi;

import java.util.List;

public interface ServiceDiscovery {
    List<String> getAvailableNodes(String serviceName);
}
