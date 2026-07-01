package com.example.memorystore.spi;

import java.util.List;

public interface MemoryStorageBackend {
    void save(String tenant, String key, String value);
    String load(String tenant, String key);
    boolean delete(String tenant, String key);
    List<String> list(String tenant);
    List<String> search(String tenant, String query);
}
