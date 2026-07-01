package com.example.auth.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;

/**
 * Refresh token request DTO with safe toString().
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record RefreshRequest(
        @NotBlank @JsonProperty("refresh_token") String refreshToken
) {
    @Override
    public String toString() {
        return "RefreshRequest[refresh_token=***]";
    }
}
