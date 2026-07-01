package com.example.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

@Configuration
@ConfigurationProperties(prefix = "agent")
public class AgentProperties {

    private String defaultModel = "qwen2.5:7b";
    private String strongModel = "qwen2.5:7b";
    private String cheapModel = "qwen2.5:7b";
    private String promptVersion = "v2";
    private String promptTenantVersionsJson = "{}";
    private boolean promptCanaryEnabled = false;
    private int promptCanaryPercent = 0;
    private String promptCanaryVersion = "";
    private String promptCanaryTenants = "";
    private boolean promptAllowRequestOverride = false;
    private String configDir = "agents";
    private int maxToolOutput = 3000;
    private int maxContextChars = 20000;
    private int maxAgentSteps = 20;
    private int llmTimeout = 60;
    private String workspace = System.getProperty("user.home") + "/agent-workspace";
    private String jwtSecret = "default-dev-secret-DO-NOT-USE-IN-PRODUCTION";
    private String memoryServerUrl = "http://localhost:8180";
    private String codeexecServerUrl = "http://localhost:8380";
    private String authServiceUrl = "http://localhost:8090";
    private String ollamaBaseUrl = "http://localhost:11434";
    private String openaiApiKey = "";
    private String anthropicApiKey = "";

    public AgentProperties() {
    }

    public AgentProperties(String defaultModel, String strongModel, String cheapModel, String promptVersion,
                           String configDir, int maxToolOutput, int maxContextChars, int maxAgentSteps,
                           String workspace, String jwtSecret, String memoryServerUrl,
                           String codeexecServerUrl, String authServiceUrl) {
        this.defaultModel = defaultModel;
        this.strongModel = strongModel;
        this.cheapModel = cheapModel;
        this.promptVersion = promptVersion;
        this.configDir = configDir;
        this.maxToolOutput = maxToolOutput;
        this.maxContextChars = maxContextChars;
        this.maxAgentSteps = maxAgentSteps;
        this.workspace = workspace;
        this.jwtSecret = jwtSecret;
        this.memoryServerUrl = memoryServerUrl;
        this.codeexecServerUrl = codeexecServerUrl;
        this.authServiceUrl = authServiceUrl;
    }

    public String defaultModel() { return defaultModel; }
    public void setDefaultModel(String defaultModel) { this.defaultModel = hasText(defaultModel) ? defaultModel : this.defaultModel; }
    public String strongModel() { return strongModel; }
    public void setStrongModel(String strongModel) { this.strongModel = hasText(strongModel) ? strongModel : defaultModel; }
    public String cheapModel() { return cheapModel; }
    public void setCheapModel(String cheapModel) { this.cheapModel = hasText(cheapModel) ? cheapModel : defaultModel; }
    public String promptVersion() { return promptVersion; }
    public void setPromptVersion(String promptVersion) { this.promptVersion = hasText(promptVersion) ? promptVersion : this.promptVersion; }
    public String promptTenantVersionsJson() { return promptTenantVersionsJson; }
    public void setPromptTenantVersionsJson(String promptTenantVersionsJson) { this.promptTenantVersionsJson = hasText(promptTenantVersionsJson) ? promptTenantVersionsJson : "{}"; }
    public boolean promptCanaryEnabled() { return promptCanaryEnabled; }
    public void setPromptCanaryEnabled(boolean promptCanaryEnabled) { this.promptCanaryEnabled = promptCanaryEnabled; }
    public int promptCanaryPercent() { return promptCanaryPercent; }
    public void setPromptCanaryPercent(int promptCanaryPercent) { this.promptCanaryPercent = Math.max(0, Math.min(100, promptCanaryPercent)); }
    public String promptCanaryVersion() { return promptCanaryVersion; }
    public void setPromptCanaryVersion(String promptCanaryVersion) { this.promptCanaryVersion = promptCanaryVersion != null ? promptCanaryVersion : ""; }
    public String promptCanaryTenants() { return promptCanaryTenants; }
    public void setPromptCanaryTenants(String promptCanaryTenants) { this.promptCanaryTenants = promptCanaryTenants != null ? promptCanaryTenants : ""; }
    public boolean promptAllowRequestOverride() { return promptAllowRequestOverride; }
    public void setPromptAllowRequestOverride(boolean promptAllowRequestOverride) { this.promptAllowRequestOverride = promptAllowRequestOverride; }
    public String configDir() { return configDir; }
    public void setConfigDir(String configDir) { this.configDir = hasText(configDir) ? configDir : this.configDir; }
    public int maxToolOutput() { return maxToolOutput; }
    public void setMaxToolOutput(int maxToolOutput) { this.maxToolOutput = maxToolOutput > 0 ? maxToolOutput : this.maxToolOutput; }
    public int maxContextChars() { return maxContextChars; }
    public void setMaxContextChars(int maxContextChars) { this.maxContextChars = maxContextChars > 0 ? maxContextChars : this.maxContextChars; }
    public int maxAgentSteps() { return maxAgentSteps; }
    public void setMaxAgentSteps(int maxAgentSteps) { this.maxAgentSteps = maxAgentSteps > 0 ? maxAgentSteps : this.maxAgentSteps; }
    public int llmTimeout() { return llmTimeout; }
    public void setLlmTimeout(int llmTimeout) { this.llmTimeout = llmTimeout > 0 ? llmTimeout : this.llmTimeout; }
    public String workspace() { return workspace; }
    @Deprecated
    public void setWorkspace(String workspace) { this.workspace = hasText(workspace) ? workspace : this.workspace; }
    public String jwtSecret() { return jwtSecret; }
    public void setJwtSecret(String jwtSecret) { this.jwtSecret = hasText(jwtSecret) ? jwtSecret : this.jwtSecret; }
    public String memoryServerUrl() { return memoryServerUrl; }
    public void setMemoryServerUrl(String memoryServerUrl) { this.memoryServerUrl = hasText(memoryServerUrl) ? memoryServerUrl : this.memoryServerUrl; }
    public String codeexecServerUrl() { return codeexecServerUrl; }
    public void setCodeexecServerUrl(String codeexecServerUrl) { this.codeexecServerUrl = hasText(codeexecServerUrl) ? codeexecServerUrl : this.codeexecServerUrl; }
    public String authServiceUrl() { return authServiceUrl; }
    public void setAuthServiceUrl(String authServiceUrl) { this.authServiceUrl = hasText(authServiceUrl) ? authServiceUrl : this.authServiceUrl; }
    public String ollamaBaseUrl() { return ollamaBaseUrl; }
    public void setOllamaBaseUrl(String ollamaBaseUrl) { this.ollamaBaseUrl = hasText(ollamaBaseUrl) ? ollamaBaseUrl : this.ollamaBaseUrl; }
    public String openaiApiKey() { return openaiApiKey; }
    public void setOpenaiApiKey(String openaiApiKey) { this.openaiApiKey = openaiApiKey; }
    public String anthropicApiKey() { return anthropicApiKey; }
    public void setAnthropicApiKey(String anthropicApiKey) { this.anthropicApiKey = anthropicApiKey; }

    private boolean hasText(String value) {
        return value != null && !value.isBlank();
    }
}
