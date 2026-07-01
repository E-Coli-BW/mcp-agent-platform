package com.example.memorystore.spi.impl;

import com.example.memorystore.spi.MemoryStorageBackend;

import java.io.IOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.util.List;
import java.util.Objects;
import java.util.stream.Collectors;

public class FileMemoryBackend implements MemoryStorageBackend {
    private final Path baseDir;

    public FileMemoryBackend(Path baseDir) {
        this.baseDir = baseDir;
    }

    private static String sanitize(String input) {
        if (input == null) return "";
        return input.replaceAll("[^A-Za-z0-9._-]", "_");
    }

    private Path safePath(String tenant, String key) {
        String tenantSafe = sanitize(tenant);
        String keySafe = sanitize(key);
        if (tenantSafe.isEmpty() || keySafe.isEmpty()) {
            throw new IllegalArgumentException("tenant and key are required");
        }
        Path full = baseDir.resolve(tenantSafe).resolve(keySafe + ".json")
                .toAbsolutePath().normalize();
        Path tenantBase = baseDir.toAbsolutePath().normalize().resolve(tenantSafe);
        if (!full.startsWith(tenantBase)) {
            throw new SecurityException("Path escapes tenant directory: " + full);
        }
        return full;
    }

    @Override
    public void save(String tenant, String key, String value) {
        Path file = safePath(tenant, key);
        try {
            Files.createDirectories(file.getParent());
            Path tmp = Files.createTempFile(file.getParent(), ".tmp-" + sanitize(key) + "-", ".json");
            Files.writeString(tmp, value, StandardOpenOption.WRITE);
            try {
                Files.move(tmp, file, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    @Override
    public String load(String tenant, String key) {
        Path file = safePath(tenant, key);
        try {
            if (!Files.exists(file)) return null;
            return Files.readString(file);
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    @Override
    public boolean delete(String tenant, String key) {
        Path file = safePath(tenant, key);
        try {
            return Files.deleteIfExists(file);
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    @Override
    public List<String> list(String tenant) {
        String tenantSafe = sanitize(tenant);
        if (tenantSafe.isEmpty()) return List.of();
        Path tenantDir = baseDir.resolve(tenantSafe).toAbsolutePath().normalize();
        Path baseAbs = baseDir.toAbsolutePath().normalize();
        if (!tenantDir.startsWith(baseAbs)) return List.of();
        try {
            if (!Files.exists(tenantDir)) return List.of();
            try (var stream = Files.list(tenantDir)) {
                return stream
                        .filter(p -> p.getFileName().toString().endsWith(".json"))
                        .map(p -> p.getFileName().toString().replace(".json", ""))
                        .collect(Collectors.toList());
            }
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    @Override
    public List<String> search(String tenant, String query) {
        String tenantSafe = sanitize(tenant);
        if (tenantSafe.isEmpty()) return List.of();
        Path tenantDir = baseDir.resolve(tenantSafe).toAbsolutePath().normalize();
        Path baseAbs = baseDir.toAbsolutePath().normalize();
        if (!tenantDir.startsWith(baseAbs)) return List.of();
        try {
            if (!Files.exists(tenantDir)) return List.of();
            try (var stream = Files.list(tenantDir)) {
                return stream
                        .filter(p -> p.getFileName().toString().endsWith(".json"))
                        .map(p -> {
                            try {
                                String content = Files.readString(p);
                                return content.contains(query) ? content : null;
                            } catch (IOException e) {
                                return null;
                            }
                        })
                        .filter(Objects::nonNull)
                        .collect(Collectors.toList());
            }
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }
}
