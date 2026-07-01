package com.example.filesearch.api;

import com.example.filesearch.tool.FileSearchToolService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

import static com.example.mcp.common.security.ToolBridgeSupport.execute;

/**
 * REST bridge for agent-server → file search tool calls.
 *
 * <p>The Python agent dispatches tool calls by POSTing to
 * {@code /api/tools/{tool_name}}. This controller maps those REST calls
 * to the underlying {@link FileSearchToolService} methods.</p>
 *
 * <p>Tenant lifecycle handled by {@code ToolBridgeSupport.execute()} which
 * sets {@code TenantContext} before invoking tool logic.</p>
 */
@RestController
@RequestMapping("/api/tools")
public class ToolBridgeController {

    private final FileSearchToolService toolService;

    public ToolBridgeController(FileSearchToolService toolService) {
        this.toolService = toolService;
    }

    @PostMapping("/file_read")
    public Map<String, String> fileRead(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_read(
                (String) params.get("path"),
                params.containsKey("startLine") ? ((Number) params.get("startLine")).intValue() : null,
                params.containsKey("endLine") ? ((Number) params.get("endLine")).intValue() : null));
    }

    @PostMapping("/file_search")
    public Map<String, String> fileSearch(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_search(
                (String) params.get("query"),
                (String) params.get("directory"),
                (String) params.get("includeGlob"),
                params.containsKey("ignoreCase") ? (Boolean) params.get("ignoreCase") : null,
                params.containsKey("limit") ? ((Number) params.get("limit")).intValue() : null));
    }

    @PostMapping("/file_list")
    public Map<String, String> fileList(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_list(
                (String) params.get("directory")));
    }

    @PostMapping("/file_tree")
    public Map<String, String> fileTree(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_tree(
                (String) params.get("directory"),
                params.containsKey("maxDepth") ? ((Number) params.get("maxDepth")).intValue() : null));
    }

    @PostMapping("/file_stat")
    public Map<String, String> fileStat(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_stat(
                (String) params.get("path")));
    }

    @PostMapping("/file_glob")
    public Map<String, String> fileGlob(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.file_glob(
                (String) params.get("pattern"),
                (String) params.get("directory"),
                params.containsKey("limit") ? ((Number) params.get("limit")).intValue() : null));
    }
}
