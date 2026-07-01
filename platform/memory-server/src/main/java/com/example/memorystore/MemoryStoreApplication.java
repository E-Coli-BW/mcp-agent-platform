package com.example.memorystore;

import com.example.memorystore.spi.impl.FileMemoryBackend;
import com.example.memorystore.spi.impl.StaticServiceDiscovery;
import java.nio.file.Paths;
import java.util.List;

public class MemoryStoreApplication {
    public static void main(String[] args) {
        // For demo: use local file backend and static discovery
        var backend = new FileMemoryBackend(Paths.get(System.getProperty("user.home"), ".mcp-local", "memory-store"));
        var discovery = new StaticServiceDiscovery(List.of("localhost:8080"));
        var service = new MemoryStoreService(backend, discovery);

        // Example usage
        service.save("tenant1", "key1", "value1");
        System.out.println("Loaded: " + service.load("tenant1", "key1"));
        System.out.println("Search: " + service.search("tenant1", "value"));
        System.out.println("Nodes: " + service.getAvailableNodes());
    }
}
