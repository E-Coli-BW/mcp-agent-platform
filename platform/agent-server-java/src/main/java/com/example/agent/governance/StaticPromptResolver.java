package com.example.agent.governance;

import com.example.agent.agent.PromptService;
import com.example.agent.config.AgentProperties;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Local prompt resolver with policy controls.
 *
 * <p>Policy precedence:</p>
 * <ol>
 *   <li>request override (if enabled)</li>
 *   <li>tenant override map</li>
 *   <li>canary policy</li>
 *   <li>default prompt version</li>
 * </ol>
 */
@Component
public class StaticPromptResolver implements PromptResolver {

    private final AgentProperties properties;
    private final PromptService promptService;
    private final ObjectMapper objectMapper;

    public StaticPromptResolver(AgentProperties properties, PromptService promptService) {
        this.properties = properties;
        this.promptService = promptService;
        this.objectMapper = new ObjectMapper();
    }

    @Override
    public PromptResolution resolve(String tenantId, String sessionId, String requestedVersion) {
        String tid = (tenantId == null || tenantId.isBlank()) ? "default" : tenantId;
        String sid = (sessionId == null || sessionId.isBlank()) ? tid : sessionId;

        String selectedVersion = normalizeVersion(properties.promptVersion());
        String source = "default";
        String rolloutPolicyId = null;

        if (isValidVersion(requestedVersion) && properties.promptAllowRequestOverride()) {
            selectedVersion = requestedVersion;
            source = "request_override";
        } else {
            String tenantOverride = tenantOverrides().get(tid);
            if (isValidVersion(tenantOverride)) {
                selectedVersion = tenantOverride;
                source = "tenant_override";
            } else if (properties.promptCanaryEnabled()
                    && properties.promptCanaryPercent() > 0
                    && isValidVersion(properties.promptCanaryVersion())
                    && canaryTenantAllowed(tid)
                    && stableBucket(tid + ":" + sid) < properties.promptCanaryPercent()) {
                selectedVersion = properties.promptCanaryVersion();
                source = "canary";
                rolloutPolicyId = "prompt-canary:" + selectedVersion + ":" + properties.promptCanaryPercent();
            }
        }

        String content = promptService.getSystemPromptByVersion(selectedVersion);
        String hash = promptService.promptHash(content);
        return new PromptResolution(
                "coding-agent.system",
                selectedVersion,
                content,
                hash,
                source,
                rolloutPolicyId
        );
    }

    private boolean isValidVersion(String value) {
        return "v1".equals(value) || "v2".equals(value);
    }

    private String normalizeVersion(String value) {
        return isValidVersion(value) ? value : "v2";
    }

    private Map<String, String> tenantOverrides() {
        String raw = properties.promptTenantVersionsJson();
        if (raw == null || raw.isBlank() || "{}".equals(raw.trim())) {
            return Map.of();
        }
        try {
            Map<String, String> parsed = objectMapper.readValue(raw, new TypeReference<Map<String, String>>() {});
            return parsed != null ? parsed : Map.of();
        } catch (Exception e) {
            return Map.of();
        }
    }

    private boolean canaryTenantAllowed(String tenantId) {
        String raw = properties.promptCanaryTenants();
        if (raw == null || raw.isBlank()) {
            return true;
        }
        Set<String> allow = Arrays.stream(raw.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .collect(Collectors.toSet());
        return allow.contains(tenantId);
    }

    private int stableBucket(String seed) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(seed.getBytes(StandardCharsets.UTF_8));
            int value = ((bytes[0] & 0xff) << 24)
                    | ((bytes[1] & 0xff) << 16)
                    | ((bytes[2] & 0xff) << 8)
                    | (bytes[3] & 0xff);
            return Math.floorMod(value, 100);
        } catch (NoSuchAlgorithmException e) {
            return 0;
        }
    }
}

