package com.example.agent.tools;

import com.example.agent.config.AgentProperties;
import com.example.mcp.common.security.TenantContext;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.lang.Nullable;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

@Configuration
public class CodeExecTools {

    private final McpRestClient codeExecClient;

    public CodeExecTools(AgentProperties agentProperties, @Nullable AuthServiceClient authClient) {
        this.codeExecClient = new McpRestClient(
            agentProperties.codeexecServerUrl(),
            agentProperties.jwtSecret(),
            Duration.ofSeconds(30),
            authClient,
            "codeexec-server"
        );
    }

    @Bean
    public FunctionToolCallback<CodeRunInput, String> codeRun() {
        return FunctionToolCallback.<CodeRunInput, String>builder(
                "code_run",
                (CodeRunInput input) -> codeRun(input)
            )
            .description("Execute code in the remote sandbox.")
            .inputType(CodeRunInput.class)
            .build();
    }

    @Bean
    public FunctionToolCallback<CodeShellInput, String> codeShell() {
        return FunctionToolCallback.<CodeShellInput, String>builder(
                "code_shell",
                (CodeShellInput input) -> codeShell(input)
            )
            .description("Execute a shell command in the remote sandbox.")
            .inputType(CodeShellInput.class)
            .build();
    }

    private String codeRun(CodeRunInput input) {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("code", input.code());
        if (input.language() != null && !input.language().isBlank()) {
            args.put("language", input.language());
        }
        if (input.timeout() != null) {
            args.put("timeout", input.timeout());
        }
        return call("code_run", args);
    }

    private String codeShell(CodeShellInput input) {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("command", input.command());
        return call("code_shell", args);
    }

    private String call(String toolName, Map<String, Object> args) {
        return codeExecClient.callTool(toolName, args, TenantContext.getOrNull())
            .blockOptional()
            .orElse("❌ Service unavailable: " + toolName);
    }

    public record CodeRunInput(String code, String language, Integer timeout) {}

    public record CodeShellInput(String command) {}
}
