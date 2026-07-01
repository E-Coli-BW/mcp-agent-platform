package com.example.codeexec.tool;

import com.example.codeexec.model.ExecutionResult;
import com.example.codeexec.sandbox.CodeSandbox;
import com.example.mcp.common.security.TenantContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.tool.annotation.Tool;
import org.springframework.ai.tool.annotation.ToolParam;
import org.springframework.stereotype.Service;

/**
 * MCP tool definitions for code execution.
 */
@Service
public class CodeExecToolService {

    private static final Logger log = LoggerFactory.getLogger(CodeExecToolService.class);
    private final CodeSandbox sandbox;

    public CodeExecToolService(CodeSandbox sandbox) {
        this.sandbox = sandbox;
    }

    @Tool(description = "Execute a code snippet in a sandboxed environment. Supports python, shell, javascript. Returns stdout, stderr, and exit code.")
    public String code_run(
            @ToolParam(description = "Code to execute") String code,
            @ToolParam(description = "Language: python, shell, or javascript", required = false) String language,
            @ToolParam(description = "Timeout in seconds (max 30)", required = false) Integer timeout) {
        try {
            String tid = TenantContext.get();
            log.info("code_run: tenant={}, language={}, codeLength={}", tid, language, code != null ? code.length() : 0);

            ExecutionResult result = sandbox.execute(tid, code, language, timeout);
            return formatResult(result);
        } catch (Exception e) {
            log.error("code_run failed", e);
            return "❌ Execution error: " + e.getMessage();
        }
    }

    @Tool(description = "Execute a shell command. Shorthand for code_run with language=shell.")
    public String code_shell(
            @ToolParam(description = "Shell command to execute") String command,
            @ToolParam(description = "Timeout in seconds (max 30)", required = false) Integer timeout) {
        return code_run(command, "shell", timeout);
    }

    private String formatResult(ExecutionResult r) {
        var sb = new StringBuilder();

        if (r.timedOut()) {
            sb.append("⏱️ Execution timed out after ").append(r.durationMs()).append("ms\n\n");
        } else if (r.exitCode() == 0) {
            sb.append("✅ Executed successfully (").append(r.durationMs()).append("ms, ").append(r.language()).append(")\n\n");
        } else {
            sb.append("❌ Exit code: ").append(r.exitCode()).append(" (").append(r.durationMs()).append("ms)\n\n");
        }

        if (!r.stdout().isBlank()) {
            sb.append("**stdout:**\n```\n").append(r.stdout()).append("\n```\n\n");
        }

        if (!r.stderr().isBlank()) {
            sb.append("**stderr:**\n```\n").append(r.stderr()).append("\n```\n");
        }

        return sb.toString();
    }
}
