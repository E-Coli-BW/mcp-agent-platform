package com.example.agent.tools;

import com.example.agent.config.AgentProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.locks.ReentrantReadWriteLock;

@Configuration
public class FileTools {

    private static final Set<String> IGNORE = Set.of(
        ".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".DS_Store",
        "tmp-m2-repo", ".mypy_cache", ".pytest_cache", ".gradle", "build", "target",
        ".sonic", ".venv-debug", ".tox", "dist", "egg-info"
    );

    private static final List<String> SEARCH_INCLUDES = List.of(
        "--include=*.py", "--include=*.java", "--include=*.js", "--include=*.ts",
        "--include=*.json", "--include=*.yaml", "--include=*.yml", "--include=*.md",
        "--include=*.html", "--include=*.css", "--include=*.xml", "--include=*.sql",
        "--include=*.sh", "--include=*.toml", "--include=*.txt", "--include=*.cfg"
    );

    private final Path workspaceRoot;

    /**
     * Per-file read-write locks to prevent concurrent write races.
     *
     * <p>Reads acquire a shared (read) lock — multiple readers can proceed in parallel.
     * Writes (file_write, file_edit) acquire an exclusive (write) lock — only one writer
     * at a time per file path, and no concurrent readers during the write.</p>
     *
     * <p>This prevents the classic TOCTOU race in file_edit (read-modify-write):
     * without this, two parallel subagents editing the same file can lose each other's changes.</p>
     *
     * <p>Keyed by normalized absolute path string. Uses ConcurrentHashMap.computeIfAbsent
     * for lock-free creation of new entries.</p>
     */
    private final ConcurrentHashMap<String, ReentrantReadWriteLock> fileLocks = new ConcurrentHashMap<>();

    private ReentrantReadWriteLock lockFor(Path path) {
        return fileLocks.computeIfAbsent(path.toAbsolutePath().normalize().toString(),
                k -> new ReentrantReadWriteLock());
    }

    public FileTools(AgentProperties agentProperties) {
        this.workspaceRoot = Paths.get(agentProperties.workspace()).toAbsolutePath().normalize();
    }

    @Bean
    public FunctionToolCallback<FileReadInput, String> fileRead() {
        return FunctionToolCallback.<FileReadInput, String>builder(
                "file_read",
                (FileReadInput input) -> fileRead(input)
            )
            .description("Read the contents of a file with line numbers.")
            .inputType(FileReadInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<FileWriteInput, String> fileWrite() {
        return FunctionToolCallback.<FileWriteInput, String>builder(
                "file_write",
                (FileWriteInput input) -> fileWrite(input)
            )
            .description("Write content to a file inside the workspace.")
            .inputType(FileWriteInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<FileEditInput, String> fileEdit() {
        return FunctionToolCallback.<FileEditInput, String>builder(
                "file_edit",
                (FileEditInput input) -> fileEdit(input)
            )
            .description("Replace the first occurrence of text in a file.")
            .inputType(FileEditInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<FileListInput, String> fileList() {
        return FunctionToolCallback.<FileListInput, String>builder(
                "file_list",
                (FileListInput input) -> fileList(input)
            )
            .description("List files and directories in the workspace.")
            .inputType(FileListInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<FileSearchInput, String> fileSearch() {
        return FunctionToolCallback.<FileSearchInput, String>builder(
                "file_search",
                (FileSearchInput input) -> fileSearch(input)
            )
            .description("Search for text in workspace files using grep.")
            .inputType(FileSearchInput.class)
            .build();
    }

    private String fileRead(FileReadInput input) {
        try {
            Path resolved = resolveWorkspacePath(input.path());
            if (!Files.isRegularFile(resolved)) {
                return "❌ File not found: " + input.path();
            }
            // Acquire shared read lock — multiple readers can proceed concurrently,
            // but writers are blocked while any reader holds the lock.
            var lock = lockFor(resolved).readLock();
            lock.lock();
            try {
                List<String> allLines = Files.readAllLines(resolved, StandardCharsets.UTF_8);
                int total = allLines.size();
                int start = Math.max(0, Math.min((input.startLine() != null ? input.startLine() : 1) - 1, total));
                int end = input.endLine() != null
                    ? Math.max(start, Math.min(input.endLine(), total))
                    : Math.min(start + 100, total);

                StringBuilder result = new StringBuilder();
                result.append("File: ")
                    .append(input.path())
                    .append(" (")
                    .append(total)
                    .append(" lines total, showing ")
                    .append(start + 1)
                    .append("-")
                    .append(end)
                    .append(")\n");
                if (end < total && input.endLine() == null) {
                    result.append("⚠️ Showing first 100 lines. Use file_read('")
                        .append(input.path())
                        .append("', start_line=")
                        .append(end + 1)
                        .append(", end_line=")
                        .append(Math.min(end + 100, total))
                        .append(") to read more.\n");
                }
                for (int index = start; index < end; index++) {
                    result.append(String.format("%4d | %s%n", index + 1, allLines.get(index)));
                }
                return result.toString();
            } finally {
                lock.unlock();
            }
        } catch (Exception ex) {
            return "❌ Failed to read " + input.path() + ": " + ex.getMessage();
        }
    }

    private String fileWrite(FileWriteInput input) {
        try {
            String content = input.content() != null ? input.content() : "";
            Path resolved = resolveWorkspacePath(input.path());
            Path parent = resolved.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            // Acquire exclusive write lock — blocks all readers and other writers.
            var lock = lockFor(resolved).writeLock();
            lock.lock();
            try {
                Files.writeString(resolved, content, StandardCharsets.UTF_8);
            } finally {
                lock.unlock();
            }
            long lines = content.chars().filter(ch -> ch == '\n').count() + 1;
            return "✅ Written " + lines + " lines to " + input.path();
        } catch (Exception ex) {
            return "❌ Failed to write " + input.path() + ": " + ex.getMessage();
        }
    }

    private String fileEdit(FileEditInput input) {
        try {
            Path resolved = resolveWorkspacePath(input.path());
            if (!Files.exists(resolved)) {
                return "❌ File not found: " + input.path();
            }
            // Acquire exclusive write lock for the entire read-modify-write operation.
            // This prevents the TOCTOU race where two parallel subagents read the same
            // content, both modify it independently, and one's changes are lost.
            var lock = lockFor(resolved).writeLock();
            lock.lock();
            try {
                String content = Files.readString(resolved, StandardCharsets.UTF_8);
                if (!content.contains(input.oldText())) {
                    String preview = content.substring(0, Math.min(200, content.length()));
                    return "❌ Text not found in " + input.path() + ". File has " + content.length() + " chars. First 200: " + preview;
                }
                int occurrences = countOccurrences(content, input.oldText());
                int index = content.indexOf(input.oldText());
                String updated = content.substring(0, index) + input.newText() + content.substring(index + input.oldText().length());
                Files.writeString(resolved, updated, StandardCharsets.UTF_8);
                if (occurrences > 1) {
                    return "✅ Replaced 1 of " + occurrences + " occurrence(s) in " + input.path() + " (first match only)";
                }
                return "✅ Replaced 1 occurrence in " + input.path();
            } finally {
                lock.unlock();
            }
        } catch (Exception ex) {
            return "❌ Edit failed: " + ex.getMessage();
        }
    }

    private String fileList(FileListInput input) {
        try {
            Path target = input.directory() == null || input.directory().isBlank()
                ? resolveWorkspaceRoot()
                : resolveWorkspacePath(input.directory());
            if (!Files.isDirectory(target)) {
                return "❌ Directory not found: " + (input.directory() != null ? input.directory() : "workspace root");
            }
            int depth = input.depth() != null ? input.depth() : 3;
            String rel = resolveWorkspaceRoot().relativize(target).toString().replace('\\', '/');
            String header = rel.isEmpty() ? "📁 " + target.getFileName() + "/" : "📁 " + rel + "/";
            List<String> lines = tree(target, "", 1, depth);
            if (lines.isEmpty()) {
                return "Directory '" + (input.directory() != null ? input.directory() : ".") + "' is empty";
            }
            return header + "\n" + String.join("\n", lines);
        } catch (Exception ex) {
            return "❌ Failed to list: " + ex.getMessage();
        }
    }

    private String fileSearch(FileSearchInput input) {
        try {
            Path searchDir = input.directory() == null || input.directory().isBlank()
                ? resolveWorkspaceRoot()
                : resolveWorkspacePath(input.directory());
            if (!Files.isDirectory(searchDir)) {
                return "❌ Directory not found: " + searchDir;
            }

            List<String> command = new ArrayList<>();
            command.add("grep");
            command.add("-rn");
            command.add("-I");
            command.addAll(SEARCH_INCLUDES);
            command.add(input.query());
            command.add(".");

            CommandResult result = runCommand(command, searchDir, Duration.ofSeconds(15));
            if (result.timedOut()) {
                return "Search timed out — try a more specific query";
            }

            String output = result.stdout().trim();
            if (output.isEmpty()) {
                if (result.exitCode() == 1) {
                    return "No matches found for '" + input.query() + "' in " + (input.directory() != null ? input.directory() : "workspace");
                }
                String stderr = result.stderr().trim();
                return stderr.isEmpty() ? "No matches found for '" + input.query() + "' in " + (input.directory() != null ? input.directory() : "workspace")
                    : "❌ Search failed: " + stderr;
            }

            String[] matches = output.split("\\R");
            if (matches.length > 50) {
                return String.join("\n", java.util.Arrays.copyOf(matches, 50))
                    + "\n... (" + (matches.length - 50) + " more matches)";
            }
            return output;
        } catch (Exception ex) {
            return "❌ Search failed: " + ex.getMessage();
        }
    }

    private List<String> tree(Path directory, String prefix, int currentDepth, int maxDepth) throws IOException {
        if (currentDepth > maxDepth) {
            return List.of();
        }

        List<Path> entries;
        try (var stream = Files.list(directory)) {
            entries = stream
                .filter(path -> shouldInclude(path.getFileName().toString()))
                .sorted(Comparator.comparing((Path path) -> Files.isDirectory(path) ? 0 : 1)
                    .thenComparing(path -> path.getFileName().toString()))
                .toList();
        }

        List<String> lines = new ArrayList<>();
        for (int index = 0; index < entries.size(); index++) {
            Path entry = entries.get(index);
            boolean isLast = index == entries.size() - 1;
            String connector = isLast ? "└── " : "├── ";
            if (Files.isDirectory(entry)) {
                lines.add(prefix + connector + "📁 " + entry.getFileName() + "/");
                String extension = isLast ? "    " : "│   ";
                lines.addAll(tree(entry, prefix + extension, currentDepth + 1, maxDepth));
            } else {
                long size = Files.size(entry);
                lines.add(prefix + connector + "📄 " + entry.getFileName() + "  (" + size + "B)");
            }
        }
        return lines;
    }

    private boolean shouldInclude(String name) {
        return !name.startsWith(".") && !IGNORE.contains(name);
    }

    private Path resolveWorkspaceRoot() throws IOException {
        Files.createDirectories(workspaceRoot);
        return workspaceRoot.toRealPath();
    }

    private Path resolveWorkspacePath(String rawPath) throws IOException {
        if (rawPath == null || rawPath.isBlank()) {
            throw new IllegalArgumentException("Path cannot be blank");
        }

        Path root = resolveWorkspaceRoot();
        Path candidate = Paths.get(rawPath);
        candidate = candidate.isAbsolute() ? candidate : root.resolve(rawPath);
        candidate = candidate.normalize().toAbsolutePath();

        Path existing = candidate;
        while (existing != null && !Files.exists(existing)) {
            existing = existing.getParent();
        }
        if (existing == null) {
            throw new IllegalArgumentException("Path '" + rawPath + "' is outside workspace '" + root + "'");
        }

        Path realExisting = existing.toRealPath();
        if (!realExisting.startsWith(root)) {
            throw new IllegalArgumentException("Path '" + rawPath + "' is outside workspace '" + root + "'");
        }

        Path resolved = realExisting;
        for (Path part : existing.relativize(candidate)) {
            resolved = resolved.resolve(part.toString()).normalize();
        }
        if (!resolved.startsWith(root)) {
            throw new IllegalArgumentException("Path '" + rawPath + "' is outside workspace '" + root + "'");
        }
        return resolved;
    }

    private int countOccurrences(String content, String needle) {
        int count = 0;
        int index = 0;
        while ((index = content.indexOf(needle, index)) >= 0) {
            count++;
            index += needle.length();
        }
        return count;
    }

    private CommandResult runCommand(List<String> command, Path cwd, Duration timeout) throws IOException, InterruptedException {
        Process process = new ProcessBuilder(command)
            .directory(cwd.toFile())
            .start();

        CompletableFuture<String> stdout = CompletableFuture.supplyAsync(() -> readStream(process.getInputStream()));
        CompletableFuture<String> stderr = CompletableFuture.supplyAsync(() -> readStream(process.getErrorStream()));
        boolean finished = process.waitFor(timeout.toMillis(), TimeUnit.MILLISECONDS);
        if (!finished) {
            process.destroyForcibly();
            return new CommandResult(-1, stdout.getNow(""), stderr.getNow(""), true);
        }
        return new CommandResult(process.exitValue(), stdout.join(), stderr.join(), false);
    }

    private String readStream(InputStream stream) {
        try (stream) {
            return new String(stream.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException ex) {
            return "";
        }
    }

    private record CommandResult(int exitCode, String stdout, String stderr, boolean timedOut) {}

    public record FileReadInput(
        String path,
        @JsonProperty("start_line") Integer startLine,
        @JsonProperty("end_line") Integer endLine
    ) {}

    public record FileWriteInput(String path, String content) {}

    public record FileEditInput(
        String path,
        @JsonProperty("old_text") String oldText,
        @JsonProperty("new_text") String newText
    ) {}

    public record FileListInput(String directory, Integer depth) {}

    public record FileSearchInput(String query, String directory) {}
}
