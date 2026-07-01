package com.example.auth.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;

/**
 * Token request DTO — replaces @RequestParam to prevent credential leakage.
 *
 * <p>SECURITY: toString() redacts password and client_secret to prevent
 * accidental logging of credentials in stack traces, debug output, or
 * Spring error pages.</p>
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record TokenRequest(
        @JsonProperty("grant_type") String grantType,
        @JsonProperty("client_id") String clientId,
        @JsonProperty("client_secret") String clientSecret,
        String username,
        String password,
        String audience,
        @JsonProperty("tenant_id") String tenantId
) {
    /**
     * Redacted toString — never exposes password or client_secret.
     */
    @Override
    public String toString() {
        return "TokenRequest[grant_type=" + grantType
                + ", client_id=" + clientId
                + ", username=" + username
                + ", audience=" + audience
                + ", tenant_id=" + tenantId
                + ", client_secret=***, password=***]";
    }
}
