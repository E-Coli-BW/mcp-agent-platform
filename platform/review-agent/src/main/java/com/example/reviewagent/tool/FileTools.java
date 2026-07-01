package com.example.reviewagent.tool;

import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * File tools for the review agent — read-only access to the workspace.
 *
 * These are Spring AI @Tool beans, automatically registered with ChatClient.
 * The agent calls them via function calling (Ollama native tool API).
 */
@Component
public class FileTools {

    private volatile String workspaceRoot = System.getProperty("user.home");

    public void setWorkspaceRoot(String root) {
        this.workspaceRoot = root;
    }

    public String getWorkspaceRoot() {
        return workspaceRoot;
    }

    @Tool(description = "List files and directories in the workspace. Returns a tree-like structure.")
    public String file_list(
            @ToolParam(description = "Relative path within workspace (default: root)", required = false) String path,
            @ToolParam(description = "Max depth to recurse (default: 3)", required = false) Integer maxDepth) {

        Path root = resolvePath(path != null ? path : "");
        int depth = maxDepth != null ? maxDepth : 3;

        if (!Files.isDirectory(root)) {
            return "❌ Not a directory: " + root;
        }

        StringBuilder sb = new StringBuilder();
        try {
            buildTree(root, root, sb, 0, depth);
        } catch (IOException e) {
            return "❌ Error listing files: " + e.getMessage();
        }
        return sb.toString();
    }

    @Tool(description = "Read a file's content with line numbers. Defaults to first 100 lines.")
    public String file_read(
            @ToolParam(description = "File path relative to workspace root") String path,
            @ToolParam(description = "Start line (1-based, default: 1)", required = false) Integer startLine,
            @ToolParam(description = "End line (default: startLine + 100)", required = false) Integer endLine) {

        Path file = resolvePath(path);
        if (!Files.isRegularFile(file)) {
            return "❌ File not found: " + path;
        }

        try {
            var lines = Files.readAllLines(file);
            int start = Math.max(0, (startLine != null ? startLine : 1) - 1);
            int end = Math.min(lines.size(), endLine != null ? endLine : start + 100);

            StringBuilder sb = new StringBuilder();
            sb.append(String.format("📄 %s (%d lines total, showing %d-%d)\n", path, lines.size(), start + 1, end));
            for (int i = start; i < end; i++) {
                sb.append(String.format("%4d | %s\n", i + 1, lines.get(i)));
            }
            if (end < lines.size()) {
                sb.append(String.format("\n... %d more lines. Use startLine/endLine to read more.\n", lines.size() - end));
            }
            return sb.toString();
        } catch (IOException e) {
            return "❌ Error reading file: " + e.getMessage();
        }
    }

    @Tool(description = "Search for a text pattern in files. Returns matching lines with file paths.")
    public String file_search(
            @ToolParam(description = "Text or regex pattern to search for") String query,
            @ToolParam(description = "File extension filter (e.g., 'java', 'py')", required = false) String fileExtension) {

        Path root = Path.of(workspaceRoot);
        StringBuilder sb = new StringBuilder();
        int matchCount = 0;

        try (Stream<Path> walk = Files.walk(root, 5)) {
            var files = walk.filter(Files::isRegularFile)
                    .filter(p -> !p.toString().contains(".git/"))
                    .filter(p -> !p.toString().contains("target/"))
                    .filter(p -> !p.toString().contains("__pycache__"))
                    .filter(p -> fileExtension == null || p.toString().endsWith("." + fileExtension))
                    .collect(Collectors.toList());

            for (Path file : files) {
                try {
                    var lines = Files.readAllLines(file);
                    for (int i = 0; i < lines.size() && matchCount < 30; i++) {
                        if (lines.get(i).toLowerCase().contains(query.toLowerCase())) {
                            String rel = root.relativize(file).toString();
                            sb.append(String.format("%s:%d: %s\n", rel, i + 1, lines.get(i).trim()));
                            matchCount++;
                        }
                    }
                } catch (IOException ignored) {}
            }
        } catch (IOException e) {
            return "❌ Error searching: " + e.getMessage();
        }

        if (matchCount == 0) return "🔍 No matches for: " + query;
        return String.format("🔍 Found %d matches for '%s':\n%s", matchCount, query, sb);
    }

    private Path resolvePath(String relativePath) {
        Path resolved = Path.of(workspaceRoot).resolve(relativePath).normalize();
        // Sandbox: prevent path traversal
        if (!resolved.startsWith(workspaceRoot)) {
            throw new SecurityException("Path traversal blocked: " + relativePath);
        }
        return resolved;
    }

    private void buildTree(Path root, Path current, StringBuilder sb, int depth, int maxDepth) throws IOException {
        if (depth > maxDepth) return;
        try (var stream = Files.list(current).sorted()) {
            for (Path entry : stream.collect(Collectors.toList())) {
                String name = entry.getFileName().toString();
                if (name.startsWith(".") || name.equals("target") || name.equals("__pycache__") || name.equals("node_modules")) continue;
                String indent = "  ".repeat(depth);
                if (Files.isDirectory(entry)) {
                    sb.append(indent).append("📁 ").append(name).append("/\n");
                    buildTree(root, entry, sb, depth + 1, maxDepth);
                } else {
                    sb.append(indent).append("📄 ").append(name).append("\n");
                }
            }
        }
    }
}
