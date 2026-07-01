package com.example.filesearch.sandbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Per-tenant filesystem sandbox.
 *
 * Security: prevents path traversal attacks by resolving symlinks and
 * checking that the real path is within the tenant's allowed root.
 *
 * MVP: all tenants share the same root directory.
 * Production: per-tenant path allowlists from config/DB.
 */
@Component
public class PathSandbox {

    private static final Logger log = LoggerFactory.getLogger(PathSandbox.class);

    private final Path defaultRoot;

    /** Per-tenant root overrides (future: load from DB/config) */
    private final Map<String, List<Path>> tenantRoots = new ConcurrentHashMap<>();

    public PathSandbox(@Value("${filesearch.default-root:/tmp}") String defaultRoot) {
        Path raw = Path.of(defaultRoot).toAbsolutePath().normalize();
        // Resolve symlinks on the root itself (macOS /var → /private/var)
        try { raw = raw.toRealPath(); } catch (IOException ignored) {}
        this.defaultRoot = raw;
        log.info("PathSandbox initialized with default root: {}", this.defaultRoot);
    }

    /**
     * Resolve and validate a path within the tenant's sandbox.
     * @throws SecurityException if the path escapes the sandbox
     */
    public Path resolve(String tenantId, String userPath) {
        Path resolved = resolveRaw(userPath);

        List<Path> allowedRoots = tenantRoots.getOrDefault(tenantId, List.of(defaultRoot));

        for (Path allowedRoot : allowedRoots) {
            if (resolved.startsWith(allowedRoot)) {
                return resolved;
            }
        }

        log.warn("SECURITY: Path traversal attempt by tenant={}, path={}, resolved={}",
                tenantId, userPath, resolved);
        throw new SecurityException("Access denied: path '" + userPath + "' is outside your sandbox");
    }

    /**
     * Check if a path is within the tenant's sandbox without throwing.
     */
    public boolean isAllowed(String tenantId, String userPath) {
        try {
            resolve(tenantId, userPath);
            return true;
        } catch (SecurityException e) {
            return false;
        }
    }

    /**
     * Get the default root for a tenant (for listing, tree commands).
     */
    public Path getRoot(String tenantId) {
        List<Path> roots = tenantRoots.getOrDefault(tenantId, List.of(defaultRoot));
        return roots.get(0);
    }

    /** Register per-tenant root paths (for future multi-tenant config). */
    public void registerTenantRoots(String tenantId, List<Path> roots) {
        tenantRoots.put(tenantId, roots.stream()
                .map(p -> {
                    Path abs = p.toAbsolutePath().normalize();
                    try { abs = abs.toRealPath(); } catch (IOException ignored) {}
                    return abs;
                })
                .toList());
    }

    /**
     * Resolve the user-supplied path to its canonical absolute form.
     *
     * <p>Symlink-escape protection: if any component of the path is a symlink
     * pointing outside the sandbox, the resolved canonical path will reflect
     * the target and the subsequent {@code startsWith} check in
     * {@link #resolve} will reject it.</p>
     *
     * <p>Non-existent leaves: callers may legitimately request paths that do
     * not yet exist (e.g. a file the agent is about to create, or a probe
     * for a missing file that should return a friendly "not found"). We
     * walk up the path until we find an existing ancestor, call
     * {@code toRealPath()} on the ancestor (so any symlinks above the
     * missing leaf are still resolved), and re-attach the missing
     * segments. The result is then containment-checked by {@link #resolve}.</p>
     *
     * @throws SecurityException if no existing ancestor can be canonicalised
     *         (extremely rare — would require an unreadable root filesystem)
     */
    private Path resolveRaw(String userPath) {
        Path path = Path.of(userPath).toAbsolutePath().normalize();

        // Fast path: the path exists as-is — resolve it fully.
        try {
            return path.toRealPath();
        } catch (IOException ignored) {
            // Fall through to the parent-walk below.
        }

        // Parent walk: find the nearest existing ancestor, canonicalise it,
        // and re-attach the missing leaf segments.
        Path ancestor = path.getParent();
        java.util.Deque<Path> missingSegments = new java.util.ArrayDeque<>();
        missingSegments.push(path.getFileName());

        while (ancestor != null) {
            try {
                Path realAncestor = ancestor.toRealPath();
                Path result = realAncestor;
                for (Path segment : missingSegments) {
                    result = result.resolve(segment);
                }
                return result.normalize();
            } catch (IOException ignored) {
                missingSegments.push(ancestor.getFileName());
                ancestor = ancestor.getParent();
            }
        }

        log.warn("SECURITY: no canonicalisable ancestor for path={}", userPath);
        throw new SecurityException("Cannot resolve path safely: " + path);
    }
}
