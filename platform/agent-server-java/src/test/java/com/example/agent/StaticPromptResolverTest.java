package com.example.agent;

import com.example.agent.agent.PromptService;
import com.example.agent.config.AgentProperties;
import com.example.agent.context.WorkspaceDetector;
import com.example.agent.governance.PromptResolution;
import com.example.agent.governance.StaticPromptResolver;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class StaticPromptResolverTest {

    @Test
    void shouldResolveDefaultVersion_whenNoOverride() {
        AgentProperties p = new AgentProperties();
        p.setPromptVersion("v2");
        PromptService promptService = new PromptService(p, new WorkspaceDetector());
        StaticPromptResolver resolver = new StaticPromptResolver(p, promptService);

        PromptResolution r = resolver.resolve("tenant-a", "s1", null);
        assertThat(r.version()).isEqualTo("v2");
        assertThat(r.assignmentSource()).isEqualTo("default");
        assertThat(r.contentHash()).startsWith("sha256:");
    }

    @Test
    void shouldUseRequestOverride_whenEnabled() {
        AgentProperties p = new AgentProperties();
        p.setPromptVersion("v2");
        p.setPromptAllowRequestOverride(true);
        PromptService promptService = new PromptService(p, new WorkspaceDetector());
        StaticPromptResolver resolver = new StaticPromptResolver(p, promptService);

        PromptResolution r = resolver.resolve("tenant-a", "s1", "v1");
        assertThat(r.version()).isEqualTo("v1");
        assertThat(r.assignmentSource()).isEqualTo("request_override");
    }

    @Test
    void shouldUseTenantOverride_whenConfigured() {
        AgentProperties p = new AgentProperties();
        p.setPromptVersion("v2");
        p.setPromptTenantVersionsJson("{\"tenant-a\":\"v1\"}");
        PromptService promptService = new PromptService(p, new WorkspaceDetector());
        StaticPromptResolver resolver = new StaticPromptResolver(p, promptService);

        PromptResolution r = resolver.resolve("tenant-a", "s1", null);
        assertThat(r.version()).isEqualTo("v1");
        assertThat(r.assignmentSource()).isEqualTo("tenant_override");
    }

    @Test
    void shouldUseCanaryVersion_whenCanaryIsEnabledAndBucketMatches() {
        AgentProperties p = new AgentProperties();
        p.setPromptVersion("v2");
        p.setPromptCanaryEnabled(true);
        p.setPromptCanaryPercent(100);
        p.setPromptCanaryVersion("v1");
        PromptService promptService = new PromptService(p, new WorkspaceDetector());
        StaticPromptResolver resolver = new StaticPromptResolver(p, promptService);

        PromptResolution r = resolver.resolve("tenant-a", "s1", null);
        assertThat(r.version()).isEqualTo("v1");
        assertThat(r.assignmentSource()).isEqualTo("canary");
        assertThat(r.rolloutPolicyId()).startsWith("prompt-canary:");
    }
}

