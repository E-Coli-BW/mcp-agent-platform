package com.example.filesearch.security;

import com.example.mcp.common.security.McpSecurityConfigBase;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;

@Configuration
@EnableWebSecurity
public class SecurityConfig extends McpSecurityConfigBase {
    // Uses default MCP security config — no customization needed
}
