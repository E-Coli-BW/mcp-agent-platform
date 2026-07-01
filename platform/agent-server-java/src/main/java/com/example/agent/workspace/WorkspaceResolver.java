package com.example.agent.workspace;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.LinkOption;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantLock;

@Component
public class WorkspaceResolver {

    private final Path basePath;
    private final boolean multiTenant;
    private final Map<String, Path> overrides = new ConcurrentHashMap<>();
    private final ReentrantLock lock = new ReentrantLock();

    public WorkspaceResolver(
            @Value("${agent.workspace:~/agent-workspace}") String base,
            @Value("${agent.multi-tenant-workspace:true}") boolean multiTenant) {
        this.basePath = Path.of(base.replaceFirst("^~", System.getProperty("user.home")))
                .toAbsolutePath()
                .normalize();
        this.multiTenant = multiTenant;
    }

    public Path forTenant(String tenantId) {
        String safeTenantId = sanitizeTenantId(tenantId);
        Path override = overrides.get(safeTenantId);
        if (override != null) {
            return override;
        }
        if (multiTenant) {
            Path tenantDirectory = basePath.resolve(safeTenantId);
            try {
                Files.createDirectories(tenantDirectory);
            } catch (IOException ignored) {
            }
            return tenantDirectory;
        }
        return basePath;
    }

    public Path setForTenant(String tenantId, String path) {
        String safeTenantId = sanitizeTenantId(tenantId);
        Path resolved = Path.of(path.replaceFirst("^~", System.getProperty("user.home")))
                .toAbsolutePath()
                .normalize();
        lock.lock();
        try {
            overrides.put(safeTenantId, resolved);
        } finally {
            lock.unlock();
        }
        return resolved;
    }

    public boolean isMultiTenant() {
        return multiTenant;
    }

    public static Path validate(Path root, String userPath) {
        if (userPath == null || userPath.isBlank()) {
            throw new SecurityException("Path must not be null or blank");
        }
        if (Path.of(userPath).isAbsolute()) {
            throw new SecurityException("Absolute paths are not allowed: " + userPath);
        }

        Path normalizedRoot = root.toAbsolutePath().normalize();
        Path resolved = normalizedRoot.resolve(userPath).normalize();
        if (!resolved.startsWith(normalizedRoot)) {
            throw new SecurityException("Path escapes workspace: " + userPath);
        }

        try {
            Path realRoot = normalizedRoot.toRealPath();
            resolved.toRealPath(LinkOption.NOFOLLOW_LINKS);
            if (Files.isSymbolicLink(resolved)) {
                Path realTarget = resolved.toRealPath();
                if (!realTarget.startsWith(realRoot)) {
                    throw new SecurityException("Symlink escapes workspace: " + userPath);
                }
            }
            if (Files.exists(resolved)) {
                Path fullyResolved = resolved.toRealPath();
                if (!fullyResolved.startsWith(realRoot)) {
                    throw new SecurityException("Path resolves outside workspace: " + userPath);
                }
            }
        } catch (IOException e) {
            if (!resolved.startsWith(normalizedRoot)) {
                throw new SecurityException("Cannot verify path safety: " + userPath, e);
            }
        }
        return resolved;
    }

    static String sanitizeTenantId(String tenantId) {
        if (tenantId == null || tenantId.isBlank()) {
            return "default";
        }
        String sanitized = tenantId.replaceAll("[^A-Za-z0-9._-]", "_");
        if (sanitized.matches("^\\.+$")) {
            return "default";
        }
        return sanitized;
    }
}
