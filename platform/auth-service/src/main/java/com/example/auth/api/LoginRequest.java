package com.example.auth.api;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * Login request DTO with validation and safe toString().
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record LoginRequest(
        @NotBlank @Size(max = 100) String username,
        @NotBlank @Size(max = 100) String password
) {
    @Override
    public String toString() {
        return "LoginRequest[username=" + username + ", password=***]";
    }
}
