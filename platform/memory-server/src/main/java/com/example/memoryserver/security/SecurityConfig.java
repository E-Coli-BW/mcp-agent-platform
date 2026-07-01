package com.example.memoryserver.security;

import com.example.mcp.common.security.McpSecurityConfigBase;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;

/**
 * Memory server security config — extends shared base.
 * Inherits: JWT filter, CSRF disabled, stateless sessions, /dev/** + /sse/** permitAll.
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig extends McpSecurityConfigBase {
    // Uses default MCP security config
    // TenantFilterAspect and TenantConnectionAspect are separate service-specific beans
}
