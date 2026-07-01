package com.example.agent.api;

import com.example.agent.workspace.WorkspaceResolver;
import com.example.mcp.common.security.TenantContext;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.server.ServerWebExchange;

import java.io.IOException;
import java.nio.charset.MalformedInputException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

@RestController
@RequestMapping("/api/workspace")
public class WorkspaceController {

    private static final Set<String> IGNORE = Set.of(
            ".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".DS_Store",
            "tmp-m2-repo", ".mypy_cache", ".pytest_cache", "dist", ".gradle", "build", "target"
    );

    private final WorkspaceResolver workspaceResolver;

    public WorkspaceController(WorkspaceResolver workspaceResolver) {
        this.workspaceResolver = workspaceResolver;
    }

    @PostMapping("/open")
    public Map<String, Object> openWorkspace(@RequestBody Map<String, String> body, ServerWebExchange exchange) {
        String path = body.get("path");
        if (path == null || path.isBlank()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "path is required");
        }

        Path resolved = Path.of(path.replaceFirst("^~", System.getProperty("user.home")))
                .toAbsolutePath()
                .normalize();
        if (!Files.exists(resolved) || !Files.isDirectory(resolved)) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Workspace does not exist: " + resolved);
        }

        Path tenantWorkspace = workspaceResolver.setForTenant(currentTenantId(exchange), resolved.toString());
        return Map.of("status", "ok", "path", tenantWorkspace.toString());
    }

    @GetMapping("/files")
    public Map<String, Object> listFiles(
            @RequestParam(defaultValue = "") String directory,
            @RequestParam(defaultValue = "3") int depth,
            ServerWebExchange exchange) {
        Path workspaceRoot = currentWorkspace(exchange);
        Path target = directory == null || directory.isBlank()
                ? workspaceRoot
                : resolveWithinWorkspace(workspaceRoot, directory);
        if (!Files.isDirectory(target)) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Directory not found: " + directory);
        }

        return Map.of(
                "root", workspaceRoot.toString(),
                "directory", workspaceRoot.relativize(target).toString(),
                "tree", buildTree(target, workspaceRoot, Math.max(depth, 0), 0)
        );
    }

    @GetMapping("/file")
    public Map<String, Object> readFile(
            @RequestParam String path,
            @RequestParam(required = false) Integer startLine,
            @RequestParam(required = false) Integer endLine,
            ServerWebExchange exchange) {
        Path workspaceRoot = currentWorkspace(exchange);
        Path file = resolveWithinWorkspace(workspaceRoot, path);
        if (!Files.exists(file) || !Files.isRegularFile(file)) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "File not found: " + path);
        }

        try {
            List<String> lines = Files.readAllLines(file);
            int from = startLine == null ? 1 : Math.max(1, startLine);
            int to = endLine == null ? lines.size() : Math.min(lines.size(), Math.max(from, endLine));
            List<String> slice = lines.subList(from - 1, to);
            return Map.of(
                    "path", workspaceRoot.relativize(file).toString(),
                    "content", String.join("\n", slice),
                    "language", detectLanguage(file),
                    "startLine", from,
                    "endLine", to,
                    "totalLines", lines.size()
            );
        } catch (MalformedInputException e) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Binary file — cannot display", e);
        } catch (IOException e) {
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Failed to read file", e);
        }
    }

    private Path currentWorkspace(ServerWebExchange exchange) {
        return workspaceResolver.forTenant(currentTenantId(exchange));
    }

    private String currentTenantId(ServerWebExchange exchange) {
        String tenantId = TenantContext.getOrNull();
        if (tenantId != null) {
            return tenantId;
        }
        String exchangeTenantId = exchange.getAttribute("tenantId");
        if (exchangeTenantId != null && !exchangeTenantId.isBlank()) {
            return exchangeTenantId;
        }
        throw new IllegalStateException("No tenant context set — is JWT auth filter configured?");
    }

    private Path resolveWithinWorkspace(Path root, String relative) {
        try {
            return WorkspaceResolver.validate(root, relative);
        } catch (SecurityException e) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "Path outside workspace", e);
        }
    }

    private List<Map<String, Object>> buildTree(Path directory, Path root, int maxDepth, int depth) {
        if (depth > maxDepth) {
            return List.of();
        }
        try (Stream<Path> paths = Files.list(directory)) {
            return paths
                    .filter(path -> !shouldIgnore(path.getFileName().toString()))
                    .sorted(Comparator
                            .comparing((Path path) -> !Files.isDirectory(path))
                            .thenComparing(path -> path.getFileName().toString().toLowerCase()))
                    .map(path -> buildNode(path, root, maxDepth, depth))
                    .toList();
        } catch (IOException e) {
            throw new ResponseStatusException(HttpStatus.INTERNAL_SERVER_ERROR, "Failed to list workspace", e);
        }
    }

    private Map<String, Object> buildNode(Path path, Path root, int maxDepth, int depth) {
        LinkedHashMap<String, Object> node = new LinkedHashMap<>();
        node.put("name", path.getFileName().toString());
        node.put("path", root.relativize(path).toString());
        if (Files.isDirectory(path)) {
            node.put("type", "directory");
            node.put("children", buildTree(path, root, maxDepth, depth + 1));
        } else {
            node.put("type", "file");
            try {
                node.put("size", Files.size(path));
            } catch (IOException e) {
                node.put("size", 0L);
            }
        }
        return node;
    }

    private boolean shouldIgnore(String name) {
        return IGNORE.contains(name) || name.endsWith(".pyc");
    }

    private String detectLanguage(Path file) {
        String name = file.getFileName().toString().toLowerCase();
        int dot = name.lastIndexOf('.');
        String extension = dot >= 0 ? name.substring(dot + 1) : "";
        return switch (extension) {
            case "py" -> "python";
            case "js" -> "javascript";
            case "ts", "tsx" -> "typescript";
            case "java" -> "java";
            case "json" -> "json";
            case "yaml", "yml" -> "yaml";
            case "md" -> "markdown";
            case "html" -> "html";
            case "css" -> "css";
            case "sql" -> "sql";
            case "sh", "bash" -> "shell";
            case "xml" -> "xml";
            case "toml" -> "toml";
            case "rs" -> "rust";
            case "go" -> "go";
            case "rb" -> "ruby";
            case "c", "h" -> "c";
            case "cpp", "hpp" -> "cpp";
            case "kt" -> "kotlin";
            case "swift" -> "swift";
            default -> "plaintext";
        };
    }
}
