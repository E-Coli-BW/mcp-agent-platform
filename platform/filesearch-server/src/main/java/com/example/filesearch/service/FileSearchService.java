package com.example.filesearch.service;

import com.example.filesearch.sandbox.PathSandbox;
import com.example.mcp.common.util.ResultTruncator;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Core file operations — all paths validated through PathSandbox.
 */
@Service
public class FileSearchService {

    private static final Logger log = LoggerFactory.getLogger(FileSearchService.class);

    private final PathSandbox sandbox;
    private final int maxResults;
    private final int maxLinesPerRead;
    private final long maxFileSize;
    private final int maxTreeDepth;

    public FileSearchService(
            PathSandbox sandbox,
            @Value("${filesearch.max-results:100}") int maxResults,
            @Value("${filesearch.max-lines-per-read:200}") int maxLinesPerRead,
            @Value("${filesearch.max-file-size-bytes:10485760}") long maxFileSize,
            @Value("${filesearch.max-tree-depth:5}") int maxTreeDepth) {
        this.sandbox = sandbox;
        this.maxResults = maxResults;
        this.maxLinesPerRead = maxLinesPerRead;
        this.maxFileSize = maxFileSize;
        this.maxTreeDepth = maxTreeDepth;
    }

    // ── file_read ────────────────────────────────────────────────

    public String readFile(String tenantId, String filePath, Integer startLine, Integer endLine) throws IOException {
        Path path = sandbox.resolve(tenantId, filePath);

        if (!Files.exists(path)) return "❌ File not found: " + filePath;
        if (Files.isDirectory(path)) return "❌ Path is a directory: " + filePath;
        if (Files.size(path) > maxFileSize)
            return "❌ File too large (" + Files.size(path) / 1024 + "KB). Max: " + maxFileSize / 1024 + "KB";

        List<String> lines = Files.readAllLines(path);
        int start = Math.max(1, startLine != null ? startLine : 1);
        int end = Math.min(lines.size(), endLine != null ? endLine : start + maxLinesPerRead - 1);

        if (start > lines.size()) return "❌ Start line " + start + " exceeds file length (" + lines.size() + " lines)";

        var sb = new StringBuilder();
        sb.append("📄 ").append(path.getFileName()).append(" (lines ").append(start).append("-").append(end)
                .append(" of ").append(lines.size()).append(")\n\n");

        for (int i = start - 1; i < end; i++) {
            sb.append(String.format("%4d | %s\n", i + 1, lines.get(i)));
        }

        if (end < lines.size()) {
            sb.append("\n[").append(lines.size() - end).append(" more lines — use startLine/endLine to paginate]");
        }

        return ResultTruncator.truncate(sb.toString());
    }

    // ── file_search (ripgrep) ────────────────────────────────────

    public String search(String tenantId, String query, String directory,
                         String includeGlob, boolean ignoreCase, Integer limit) throws IOException {
        Path searchRoot = directory != null
                ? sandbox.resolve(tenantId, directory)
                : sandbox.getRoot(tenantId);

        int maxHits = Math.min(limit != null ? limit : 50, maxResults);

        // Try ripgrep first, fall back to Java grep
        if (isRipgrepAvailable()) {
            return ripgrepSearch(searchRoot, query, includeGlob, ignoreCase, maxHits);
        }
        return javaSearch(searchRoot, query, includeGlob, ignoreCase, maxHits);
    }

    private String ripgrepSearch(Path root, String query, String includeGlob,
                                  boolean ignoreCase, int limit) throws IOException {
        var cmd = new ArrayList<>(List.of("rg", "--line-number", "--no-heading",
                "--max-count", String.valueOf(limit), "--color", "never"));

        if (ignoreCase) cmd.add("--ignore-case");
        if (includeGlob != null) { cmd.add("--glob"); cmd.add(includeGlob); }
        cmd.add(query);
        cmd.add(root.toString());

        try {
            var process = new ProcessBuilder(cmd)
                    .directory(root.toFile())
                    .redirectErrorStream(true)
                    .start();

            String output;
            try (var reader = new BufferedReader(new InputStreamReader(process.getInputStream()))) {
                output = reader.lines().limit(limit).collect(Collectors.joining("\n"));
            }

            boolean finished = process.waitFor(10, TimeUnit.SECONDS);
            if (!finished) {
                process.destroyForcibly();
                output += "\n[Search timed out after 10s]";
            }

            if (output.isEmpty()) return "🔍 No matches found for '" + query + "'";

            long matchCount = output.lines().count();
            return "🔍 Found " + matchCount + " match(es) for '" + query + "':\n\n"
                    + ResultTruncator.truncate(output);

        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return "❌ Search interrupted";
        }
    }

    private String javaSearch(Path root, String query, String includeGlob,
                               boolean ignoreCase, int limit) throws IOException {
        String pattern = ignoreCase ? query.toLowerCase() : query;
        var matches = new ArrayList<String>();

        try (Stream<Path> walk = Files.walk(root, 10)) {
            walk.filter(Files::isRegularFile)
                .filter(p -> includeGlob == null || p.toString().matches(globToRegex(includeGlob)))
                .forEach(file -> {
                    if (matches.size() >= limit) return;
                    try {
                        List<String> lines = Files.readAllLines(file);
                        for (int i = 0; i < lines.size() && matches.size() < limit; i++) {
                            String line = lines.get(i);
                            String check = ignoreCase ? line.toLowerCase() : line;
                            if (check.contains(pattern)) {
                                matches.add(root.relativize(file) + ":" + (i + 1) + ":" + line.trim());
                            }
                        }
                    } catch (IOException ignored) {}
                });
        }

        if (matches.isEmpty()) return "🔍 No matches found for '" + query + "'";
        return "🔍 Found " + matches.size() + " match(es) for '" + query + "':\n\n"
                + ResultTruncator.truncate(String.join("\n", matches));
    }

    // ── file_list ────────────────────────────────────────────────

    public String listDirectory(String tenantId, String dirPath) throws IOException {
        Path dir = dirPath != null
                ? sandbox.resolve(tenantId, dirPath)
                : sandbox.getRoot(tenantId);

        if (!Files.isDirectory(dir)) return "❌ Not a directory: " + dirPath;

        var entries = new ArrayList<Map<String, Object>>();
        try (var stream = Files.list(dir)) {
            stream.sorted().forEach(p -> {
                try {
                    boolean isDir = Files.isDirectory(p);
                    entries.add(Map.of(
                            "name", p.getFileName().toString() + (isDir ? "/" : ""),
                            "type", isDir ? "directory" : "file",
                            "size", isDir ? 0 : Files.size(p)));
                } catch (IOException ignored) {}
            });
        }

        if (entries.isEmpty()) return "📁 Empty directory: " + dir;
        var sb = new StringBuilder("📁 " + dir + " (" + entries.size() + " entries):\n\n");
        for (var e : entries) {
            String sizeStr = "directory".equals(e.get("type")) ? "<DIR>" :
                    formatSize((long) e.get("size"));
            sb.append(String.format("  %-8s %s\n", sizeStr, e.get("name")));
        }
        return sb.toString();
    }

    // ── file_tree ────────────────────────────────────────────────

    public String tree(String tenantId, String dirPath, Integer maxDepth) throws IOException {
        Path dir = dirPath != null
                ? sandbox.resolve(tenantId, dirPath)
                : sandbox.getRoot(tenantId);

        if (!Files.isDirectory(dir)) return "❌ Not a directory: " + dirPath;

        int depth = Math.min(maxDepth != null ? maxDepth : 3, maxTreeDepth);
        var sb = new StringBuilder("🌲 " + dir.getFileName() + "/\n");
        buildTree(dir, "", depth, sb, 0);
        return ResultTruncator.truncate(sb.toString());
    }

    private int buildTree(Path dir, String prefix, int maxDepth, StringBuilder sb, int count) throws IOException {
        if (maxDepth <= 0 || count > 500) return count;
        try (var stream = Files.list(dir)) {
            var entries = stream.sorted().toList();
            for (int i = 0; i < entries.size() && count < 500; i++) {
                Path entry = entries.get(i);
                boolean isLast = (i == entries.size() - 1);
                String connector = isLast ? "└── " : "├── ";
                boolean isDir = Files.isDirectory(entry);

                sb.append(prefix).append(connector).append(entry.getFileName())
                        .append(isDir ? "/" : "").append('\n');
                count++;

                if (isDir) {
                    String childPrefix = prefix + (isLast ? "    " : "│   ");
                    count = buildTree(entry, childPrefix, maxDepth - 1, sb, count);
                }
            }
        }
        return count;
    }

    // ── file_stat ────────────────────────────────────────────────

    public String stat(String tenantId, String filePath) throws IOException {
        Path path = sandbox.resolve(tenantId, filePath);
        if (!Files.exists(path)) return "❌ File not found: " + filePath;

        BasicFileAttributes attrs = Files.readAttributes(path, BasicFileAttributes.class);
        return String.format("""
                📊 %s
                  Type:     %s
                  Size:     %s (%d bytes)
                  Modified: %s
                  Created:  %s
                  Readable: %s
                  Writable: %s""",
                path,
                attrs.isDirectory() ? "directory" : attrs.isSymbolicLink() ? "symlink" : "file",
                formatSize(attrs.size()), attrs.size(),
                Instant.ofEpochMilli(attrs.lastModifiedTime().toMillis()),
                Instant.ofEpochMilli(attrs.creationTime().toMillis()),
                Files.isReadable(path), Files.isWritable(path));
    }

    // ── file_glob ────────────────────────────────────────────────

    public String glob(String tenantId, String pattern, String directory, Integer limit) throws IOException {
        Path root = directory != null
                ? sandbox.resolve(tenantId, directory)
                : sandbox.getRoot(tenantId);

        int maxHits = Math.min(limit != null ? limit : 50, maxResults);
        PathMatcher matcher = FileSystems.getDefault().getPathMatcher("glob:" + pattern);

        var matches = new ArrayList<String>();
        try (Stream<Path> walk = Files.walk(root, 10)) {
            walk.filter(Files::isRegularFile)
                .filter(p -> matcher.matches(p.getFileName()) || matcher.matches(root.relativize(p)))
                .limit(maxHits)
                .forEach(p -> matches.add(root.relativize(p).toString()));
        }

        if (matches.isEmpty()) return "🔍 No files matching '" + pattern + "'";
        return "🔍 Found " + matches.size() + " file(s) matching '" + pattern + "':\n\n"
                + String.join("\n", matches);
    }

    // ── Helpers ──────────────────────────────────────────────────

    private boolean isRipgrepAvailable() {
        try {
            return new ProcessBuilder("rg", "--version").start().waitFor() == 0;
        } catch (Exception e) {
            return false;
        }
    }

    private String formatSize(long bytes) {
        if (bytes < 1024) return bytes + "B";
        if (bytes < 1024 * 1024) return (bytes / 1024) + "KB";
        return String.format("%.1fMB", bytes / (1024.0 * 1024));
    }

    private String globToRegex(String glob) {
        return ".*" + glob.replace(".", "\\.").replace("*", ".*").replace("?", ".") + ".*";
    }
}
