package com.example.auth.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * Client registration request DTO with validation and safe toString().
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record RegisterClientRequest(
        @NotBlank @Size(max = 100) @JsonProperty("client_id") String clientId,
        @NotBlank @Size(max = 100) @JsonProperty("client_secret") String clientSecret,
        @JsonProperty("client_name") String clientName,
        String scopes,
        @JsonProperty("tenant_id") String tenantId
) {
    @Override
    public String toString() {
        return "RegisterClientRequest[client_id=" + clientId
                + ", client_name=" + clientName
                + ", tenant_id=" + tenantId
                + ", client_secret=***]";
    }
}
