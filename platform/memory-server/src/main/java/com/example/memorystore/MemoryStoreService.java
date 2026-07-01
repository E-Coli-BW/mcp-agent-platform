package com.example.memorystore;

import com.example.memorystore.spi.MemoryStorageBackend;
import com.example.memorystore.spi.ServiceDiscovery;
import java.util.List;

public class MemoryStoreService {
    private final MemoryStorageBackend backend;
    private final ServiceDiscovery discovery;

    public MemoryStoreService(MemoryStorageBackend backend, ServiceDiscovery discovery) {
        this.backend = backend;
        this.discovery = discovery;
    }

    public void save(String tenant, String key, String value) {
        backend.save(tenant, key, value);
    }

    public String load(String tenant, String key) {
        return backend.load(tenant, key);
    }

    public boolean delete(String tenant, String key) {
        return backend.delete(tenant, key);
    }

    public List<String> list(String tenant) {
        return backend.list(tenant);
    }

    public List<String> search(String tenant, String query) {
        return backend.search(tenant, query);
    }

    public List<String> getAvailableNodes() {
        return discovery.getAvailableNodes("memory-store");
    }
}
