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
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Registry that dispatches to the right chunker based on file extension.
 */
@Component
public class ChunkerRegistry {

    private static final Logger log = LoggerFactory.getLogger(ChunkerRegistry.class);
    private static final Set<String> SKIP_DIRS = Set.of(
            ".git", "node_modules", "__pycache__", ".venv", "target", "dist", "build", "tmp-m2-repo"
    );
    private static final Map<String, String> EXTENSION_TO_TYPE = Map.ofEntries(
            Map.entry(".py", "tree_sitter"),
            Map.entry(".java", "tree_sitter"),
            Map.entry(".js", "tree_sitter"),
            Map.entry(".ts", "tree_sitter"),
            Map.entry(".tsx", "tree_sitter"),
            Map.entry(".go", "tree_sitter"),
            Map.entry(".rs", "tree_sitter"),
            Map.entry(".md", "markdown"),
            Map.entry(".yaml", "openapi_or_yaml"),
            Map.entry(".yml", "openapi_or_yaml"),
            Map.entry(".json", "openapi_or_json"),
            Map.entry(".html", "html"),
            Map.entry(".txt", "fixed_size"),
            Map.entry(".csv", "fixed_size")
    );

    private final TreeSitterChunker treeSitterChunker;
    private final MarkdownChunker markdownChunker;
    private final OpenApiChunker openApiChunker;
    private final FixedSizeChunker fixedSizeChunker;
    private final HtmlChunker htmlChunker;

    public ChunkerRegistry(TreeSitterChunker treeSitterChunker,
                           MarkdownChunker markdownChunker,
                           OpenApiChunker openApiChunker,
                           FixedSizeChunker fixedSizeChunker,
                           HtmlChunker htmlChunker) {
        this.treeSitterChunker = treeSitterChunker;
        this.markdownChunker = markdownChunker;
        this.openApiChunker = openApiChunker;
        this.fixedSizeChunker = fixedSizeChunker;
        this.htmlChunker = htmlChunker;
    }

    /**
     * Chunk a single file using the appropriate chunker.
     */
    public List<CodeChunk> chunkFile(Path filePath) {
        String ext = getExtension(filePath);
        String type = EXTENSION_TO_TYPE.getOrDefault(ext, "tree_sitter");

        return switch (type) {
            case "markdown" -> markdownChunker.chunk(filePath);
            case "openapi_or_yaml", "openapi_or_json" -> openApiChunker.chunk(filePath);
            case "html" -> htmlChunker.chunk(filePath);
            case "fixed_size" -> fixedSizeChunker.chunk(filePath);
            default -> treeSitterChunker.chunkFile(filePath);
        };
    }

    /**
     * Chunk all supported files in a directory tree.
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
                    String ext = getExtension(file);
                    if (EXTENSION_TO_TYPE.containsKey(ext)) {
                        allChunks.addAll(chunkFile(file));
                    }
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
}
