package com.example.modelrouter.config;

import com.example.modelrouter.tool.ModelRouterToolService;
import org.springframework.ai.tool.ToolCallbackProvider;
import org.springframework.ai.tool.method.MethodToolCallbackProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class McpToolConfig {
    @Bean
    public ToolCallbackProvider modelRouterToolCallbackProvider(ModelRouterToolService toolService) {
        return MethodToolCallbackProvider.builder()
                .toolObjects(toolService)
                .build();
    }
}
