package com.example.agent.context;

import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Detects workspace metadata for prompt enrichment.
 */
@Component
public class WorkspaceDetector {

    private static final Map<String, String> PROJECT_MARKERS = Map.of(
            "pom.xml", "Java/Maven",
            "build.gradle", "Java/Gradle",
            "package.json", "Node.js",
            "pyproject.toml", "Python",
            "Cargo.toml", "Rust",
            "go.mod", "Go",
            "Gemfile", "Ruby",
            "requirements.txt", "Python"
    );

    private static final List<String> KEY_FILES = List.of(
            "README.md",
            "README",
            "README.rst",
            "package.json",
            "pyproject.toml",
            "pom.xml"
    );

    private static final Set<String> MODULE_MARKERS = Set.of(
            "pom.xml",
            "package.json",
            "pyproject.toml",
            "build.gradle",
            "Cargo.toml"
    );

    private static final Set<String> SKIP_DIRS = Set.of(
            ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"
    );

    private volatile String cachedRoot;
    private volatile String cachedContext;

    /**
     * Return cached workspace context or compute it.
     *
     * @param root workspace root
     * @return workspace context string
     */
    public String getWorkspaceContext(Path root) {
        Path normalizedRoot = root.toAbsolutePath().normalize();
        String key = normalizedRoot.toString();
        if (key.equals(cachedRoot) && cachedContext != null) {
            return cachedContext;
        }

        List<String> parts = new ArrayList<>();
        Path fileName = normalizedRoot.getFileName();
        parts.add("Workspace: " + (fileName != null ? fileName : normalizedRoot));

        String projectType = detectProjectType(normalizedRoot);
        if (projectType != null) {
            parts.add("Type: " + projectType);
        }

        List<String> modules = detectModules(normalizedRoot);
        if (!modules.isEmpty()) {
            parts.add("Modules: " + String.join(", ", modules.subList(0, Math.min(10, modules.size()))));
            if (modules.size() > 10) {
                parts.add("  ... and " + (modules.size() - 10) + " more");
            }
        }

        String summary = readSummary(normalizedRoot);
        if (summary != null) {
            parts.add(summary);
        }

        String context = String.join("\n", parts);
        cachedRoot = key;
        cachedContext = context;
        return context;
    }

    /**
     * Detect the project type from root markers.
     *
     * @param root workspace root
     * @return project type or null
     */
    public String detectProjectType(Path root) {
        for (Map.Entry<String, String> entry : PROJECT_MARKERS.entrySet()) {
            if (Files.exists(root.resolve(entry.getKey()))) {
                return entry.getValue();
            }
        }
        return null;
    }

    /**
     * Detect modules inside the workspace.
     *
     * @param root workspace root
     * @return relative module paths
     */
    public List<String> detectModules(Path root) {
        List<String> modules = new ArrayList<>();
        Path normalizedRoot = root.toAbsolutePath().normalize();
        scanModules(normalizedRoot, normalizedRoot, 0, modules);
        return modules;
    }

    /**
     * Read the first 500 chars of a summary file.
     *
     * @param root workspace root
     * @return summary string or null
     */
    public String readSummary(Path root) {
        for (String fileName : KEY_FILES) {
            Path candidate = root.resolve(fileName);
            if (Files.isRegularFile(candidate)) {
                try {
                    String content = Files.readString(candidate, StandardCharsets.UTF_8);
                    return "[" + fileName + "]: " + content.substring(0, Math.min(500, content.length())).trim();
                }
                catch (IOException ignored) {
                    // graceful fallback
                }
            }
        }
        return null;
    }

    private void scanModules(Path root, Path current, int depth, List<String> modules) {
        if (depth > 2 || !Files.isDirectory(current)) {
            return;
        }
        try (var stream = Files.list(current)) {
            List<Path> children = stream.toList();
            boolean isModule = !current.equals(root)
                    && children.stream().map(path -> path.getFileName().toString()).anyMatch(MODULE_MARKERS::contains);
            if (isModule) {
                modules.add(root.relativize(current).toString().replace('\\', '/'));
            }
            for (Path child : children) {
                if (Files.isDirectory(child) && !SKIP_DIRS.contains(child.getFileName().toString())) {
                    scanModules(root, child, depth + 1, modules);
                }
            }
        }
        catch (IOException ignored) {
            // graceful fallback
        }
    }
}
