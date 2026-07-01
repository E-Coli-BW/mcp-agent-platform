package com.example.auth.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * Signup request DTO with validation and safe toString().
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record SignupRequest(
        @NotBlank @Size(min = 3, max = 100) String username,
        @NotBlank @Size(min = 8, max = 100) String password,
        String email,
        @NotBlank @Size(max = 100) @JsonProperty("tenant_id") String tenantId
) {
    @Override
    public String toString() {
        return "SignupRequest[username=" + username + ", email=" + email
                + ", tenant_id=" + tenantId + ", password=***]";
    }
}
