package com.example.filesearch.tool;

import com.example.mcp.common.security.TenantContext;
import com.example.filesearch.service.FileSearchService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * MCP tool definitions for file search operations.
 * Thin adapter: extract tenant → call service → return string.
 */
@Service
public class FileSearchToolService {

    private static final Logger log = LoggerFactory.getLogger(FileSearchToolService.class);

    private final FileSearchService fileService;

    public FileSearchToolService(FileSearchService fileService) {
        this.fileService = fileService;
    }

    @Tool(description = "Read the contents of a file. Specify startLine/endLine to read a range. Returns line numbers.")
    public String file_read(
            @ToolParam(description = "Absolute or relative file path") String path,
            @ToolParam(description = "Start line number (1-based)", required = false) Integer startLine,
            @ToolParam(description = "End line number (inclusive)", required = false) Integer endLine) {
        try {
            return fileService.readFile(TenantContext.get(), path, startLine, endLine);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_read failed: path={}", path, e);
            return "❌ Error reading file: " + e.getMessage();
        }
    }

    @Tool(description = "Search for text in files using grep/ripgrep. Returns matching lines with file paths and line numbers.")
    public String file_search(
            @ToolParam(description = "Search query (text or regex)") String query,
            @ToolParam(description = "Directory to search in", required = false) String directory,
            @ToolParam(description = "File glob pattern, e.g. '*.java'", required = false) String includeGlob,
            @ToolParam(description = "Case-insensitive search", required = false) Boolean ignoreCase,
            @ToolParam(description = "Max results to return", required = false) Integer limit) {
        try {
            return fileService.search(TenantContext.get(), query, directory,
                    includeGlob, ignoreCase != null && ignoreCase, limit);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_search failed: query={}", query, e);
            return "❌ Search error: " + e.getMessage();
        }
    }

    @Tool(description = "List contents of a directory. Shows file names, types, and sizes.")
    public String file_list(
            @ToolParam(description = "Directory path (default: workspace root)", required = false) String directory) {
        try {
            return fileService.listDirectory(TenantContext.get(), directory);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_list failed: dir={}", directory, e);
            return "❌ Error listing directory: " + e.getMessage();
        }
    }

    @Tool(description = "Show tree view of directory structure. Like the 'tree' command.")
    public String file_tree(
            @ToolParam(description = "Directory path", required = false) String directory,
            @ToolParam(description = "Max depth (default: 3)", required = false) Integer maxDepth) {
        try {
            return fileService.tree(TenantContext.get(), directory, maxDepth);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_tree failed: dir={}", directory, e);
            return "❌ Error: " + e.getMessage();
        }
    }

    @Tool(description = "Get file metadata: size, type, timestamps, permissions.")
    public String file_stat(
            @ToolParam(description = "File or directory path") String path) {
        try {
            return fileService.stat(TenantContext.get(), path);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_stat failed: path={}", path, e);
            return "❌ Error: " + e.getMessage();
        }
    }

    @Tool(description = "Find files matching a glob pattern (e.g., '*.java', '**/*.md').")
    public String file_glob(
            @ToolParam(description = "Glob pattern") String pattern,
            @ToolParam(description = "Directory to search in", required = false) String directory,
            @ToolParam(description = "Max results", required = false) Integer limit) {
        try {
            return fileService.glob(TenantContext.get(), pattern, directory, limit);
        } catch (SecurityException e) {
            return "🔒 " + e.getMessage();
        } catch (Exception e) {
            log.error("file_glob failed: pattern={}", pattern, e);
            return "❌ Error: " + e.getMessage();
        }
    }
}
