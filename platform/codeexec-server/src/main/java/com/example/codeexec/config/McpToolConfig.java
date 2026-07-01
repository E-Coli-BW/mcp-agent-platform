package com.example.codeexec.config;

import com.example.codeexec.tool.CodeExecToolService;
import org.springframework.ai.tool.ToolCallbackProvider;
import org.springframework.ai.tool.method.MethodToolCallbackProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class McpToolConfig {
    @Bean
    public ToolCallbackProvider codeExecToolCallbackProvider(CodeExecToolService toolService) {
        return MethodToolCallbackProvider.builder()
                .toolObjects(toolService)
                .build();
    }
}
