package com.example.mcp.common.security;

public class TenantSecurityException extends SecurityException {
    public TenantSecurityException(String message) {
        super(message);
    }
}
