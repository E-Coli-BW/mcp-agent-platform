package com.example.filesearch.config;

import com.example.filesearch.tool.FileSearchToolService;
import org.springframework.ai.tool.ToolCallbackProvider;
import org.springframework.ai.tool.method.MethodToolCallbackProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class McpToolConfig {
    @Bean
    public ToolCallbackProvider fileSearchToolCallbackProvider(FileSearchToolService toolService) {
        return MethodToolCallbackProvider.builder()
                .toolObjects(toolService)
                .build();
    }
}
