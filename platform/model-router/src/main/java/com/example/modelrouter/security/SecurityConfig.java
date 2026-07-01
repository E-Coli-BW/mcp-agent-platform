package com.example.modelrouter.security;

import com.example.mcp.common.security.McpSecurityConfigBase;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;

@Configuration
@EnableWebSecurity
public class SecurityConfig extends McpSecurityConfigBase {}
