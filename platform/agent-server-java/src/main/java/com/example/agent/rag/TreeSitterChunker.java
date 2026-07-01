package com.example.agent.rag;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Code chunker — extracts function/class definitions from source files.
 */
@Component
public class TreeSitterChunker {

    private static final Logger log = LoggerFactory.getLogger(TreeSitterChunker.class);
    private static final Set<String> SKIP_DIRS = Set.of(".git", "node_modules", "__pycache__", ".venv", "target", "dist", "build", ".idea");
    private static final Set<String> SUPPORTED_EXTENSIONS = Set.of(".py", ".java", ".js", ".ts", ".tsx", ".go", ".rs");
    private static final Map<String, List<Pattern>> CHUNK_PATTERNS = Map.of(
            ".py", List.of(
                    Pattern.compile("^(class\\s+\\w+[^:]*:.*?)(?=^class\\s|^def\\s|\\Z)", Pattern.MULTILINE | Pattern.DOTALL),
                    Pattern.compile("^(def\\s+\\w+\\s*\\([^)]*\\)[^:]*:.*?)(?=^def\\s|^class\\s|\\Z)", Pattern.MULTILINE | Pattern.DOTALL)),
            ".java", List.of(
                    Pattern.compile("((?:public|private|protected|static|final|abstract|synchronized)*\\s*(?:class|interface|enum|record)\\s+\\w+[^{]*\\{)", Pattern.MULTILINE),
                    Pattern.compile("((?:public|private|protected|static|final|abstract|synchronized|default)*\\s+[\\w<>\\[\\]]+\\s+\\w+\\s*\\([^)]*\\)\\s*(?:throws\\s+[\\w,\\s]+)?\\s*\\{)", Pattern.MULTILINE)),
            ".js", List.of(
                    Pattern.compile("((?:export\\s+)?(?:async\\s+)?function\\s+\\w+\\s*\\([^)]*\\)\\s*\\{)", Pattern.MULTILINE),
                    Pattern.compile("(class\\s+\\w+[^{]*\\{)", Pattern.MULTILINE)),
            ".ts", List.of(
                    Pattern.compile("((?:export\\s+)?(?:async\\s+)?function\\s+\\w+[^{]*\\{)", Pattern.MULTILINE),
                    Pattern.compile("((?:export\\s+)?class\\s+\\w+[^{]*\\{)", Pattern.MULTILINE)),
            ".tsx", List.of(
                    Pattern.compile("((?:export\\s+)?(?:async\\s+)?function\\s+\\w+[^{]*\\{)", Pattern.MULTILINE),
                    Pattern.compile("((?:export\\s+)?class\\s+\\w+[^{]*\\{)", Pattern.MULTILINE)));
    private static final int MAX_FILE_SIZE = 10 * 1024;

    /**
     * Chunks a single source file.
     */
    public List<CodeChunk> chunkFile(Path filePath) {
        String ext = getExtension(filePath);
        if (!SUPPORTED_EXTENSIONS.contains(ext)) {
            return List.of();
        }
        try {
            String content = Files.readString(filePath);
            String language = ext.substring(1);
            Instant modified = Files.getLastModifiedTime(filePath).toInstant();
            List<Pattern> patterns = CHUNK_PATTERNS.getOrDefault(ext, List.of());
            List<CodeChunk> chunks = new ArrayList<>();
            for (Pattern pattern : patterns) {
                Matcher matcher = pattern.matcher(content);
                while (matcher.find()) {
                    String match = matcher.group(1);
                    int startLine = countLines(content, 0, matcher.start()) + 1;
                    int endLine = startLine + countNewlines(match);
                    String name = extractName(match);
                    String docstring = extractDocstring(content, matcher.start(), language);
                    String chunkType = match.contains("class") ? "class" : "function";
                    chunks.add(new CodeChunk(match, filePath.toString(), language, chunkType, name, startLine, endLine, modified, docstring));
                }
            }
            if (chunks.isEmpty() && content.length() <= MAX_FILE_SIZE) {
                int totalLines = content.split("\\n").length;
                chunks.add(new CodeChunk(content, filePath.toString(), language, "module", filePath.getFileName().toString(), 1, totalLines, modified, null));
            }
            return chunks;
        } catch (IOException e) {
            log.debug("Failed to chunk {}: {}", filePath, e.getMessage());
            return List.of();
        }
    }

    /**
     * Chunks all supported source files in a directory tree.
     */
    public List<CodeChunk> chunkDirectory(Path directory) {
        List<CodeChunk> allChunks = new ArrayList<>();
        try {
            Files.walkFileTree(directory, new SimpleFileVisitor<>() {
                @Override
                public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                    if (SKIP_DIRS.contains(dir.getFileName().toString())) {
                        return FileVisitResult.SKIP_SUBTREE;
                    }
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                    allChunks.addAll(chunkFile(file));
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException e) {
            log.warn("Failed to walk directory {}: {}", directory, e.getMessage());
        }
        return allChunks;
    }

    private String getExtension(Path path) {
        String name = path.getFileName().toString();
        int dot = name.lastIndexOf('.');
        return dot >= 0 ? name.substring(dot) : "";
    }

    private int countLines(String text, int start, int end) {
        int count = 0;
        for (int i = start; i < end; i++) {
            if (text.charAt(i) == '\n') count++;
        }
        return count;
    }

    private int countNewlines(String text) {
        return (int) text.chars().filter(c -> c == '\n').count();
    }

    private String extractName(String match) {
        Matcher matcher = Pattern.compile("(?:class|def|function|interface|enum|record)\\s+(\\w+)").matcher(match);
        return matcher.find() ? matcher.group(1) : "unknown";
    }

    private String extractDocstring(String content, int matchStart, String language) {
        if ("py".equals(language)) {
            int colon = content.indexOf(':', matchStart);
            if (colon >= 0) {
                String after = content.substring(colon + 1, Math.min(colon + 500, content.length())).trim();
                if (after.startsWith("\"\"\"") || after.startsWith("'''")) {
                    String quote = after.substring(0, 3);
                    int end = after.indexOf(quote, 3);
                    if (end > 3) {
                        return after.substring(3, end).trim();
                    }
                }
            }
        }
        return null;
    }
}
