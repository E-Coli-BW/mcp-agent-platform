package com.example.codeexec.api;

import com.example.codeexec.tool.CodeExecToolService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

import static com.example.mcp.common.security.ToolBridgeSupport.execute;

/**
 * REST bridge for agent-server → code execution tool calls.
 *
 * <p>WHY THIS EXISTS: the agent-server's {@code McpToolClient} dispatches tool
 * calls by POSTing to {@code /api/tools/{tool_name}}. Without this controller
 * those requests hit {@code anyRequest().denyAll()} and return 401 — which
 * looks exactly like an auth failure but is really a missing endpoint. The
 * memory-server has had this controller for a while; codeexec was missing
 * its mirror, so {@code code_run} and {@code code_shell} were never reachable
 * from the Python agent. This file restores parity with memory-server's
 * {@code /api/tools/*} bridge.</p>
 *
 * <p>Tenant lifecycle is handled by {@link com.example.mcp.common.security.ToolBridgeSupport#execute}
 * which establishes the {@code TenantContext} before invoking the tool method
 * (the underlying service reads it via {@link com.example.mcp.common.security.TenantContext#get()}).</p>
 */
@RestController
@RequestMapping("/api/tools")
public class ToolBridgeController {

    private final CodeExecToolService toolService;

    public ToolBridgeController(CodeExecToolService toolService) {
        this.toolService = toolService;
    }

    @PostMapping("/code_run")
    public Map<String, String> codeRun(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.code_run(
                (String) params.get("code"),
                (String) params.getOrDefault("language", "python"),
                params.containsKey("timeout") ? ((Number) params.get("timeout")).intValue() : null));
    }

    @PostMapping("/code_shell")
    public Map<String, String> codeShell(@RequestBody Map<String, Object> params) {
        return execute(() -> toolService.code_shell(
                (String) params.get("command"),
                params.containsKey("timeout") ? ((Number) params.get("timeout")).intValue() : null));
    }
}
