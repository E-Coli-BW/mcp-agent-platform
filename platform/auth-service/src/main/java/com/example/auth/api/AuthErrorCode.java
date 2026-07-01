package com.example.auth.api;

/**
 * OAuth2/auth error codes — eliminates magic strings in controllers.
 *
 * <p>Alibaba Constant Definition #1: magic values must not appear directly in code</p>
 */
public final class AuthErrorCode {

    public static final String INVALID_REQUEST = "invalid_request";
    public static final String INVALID_CLIENT = "invalid_client";
    public static final String INVALID_CREDENTIALS = "invalid_credentials";
    public static final String INVALID_TOKEN = "invalid_token";
    public static final String UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type";
    public static final String FORBIDDEN = "forbidden";
    public static final String RATE_LIMITED = "rate_limited";
    public static final String ACCOUNT_LOCKED = "account_locked";
    public static final String ACCOUNT_DISABLED = "account_disabled";

    public static final String DEFAULT_AUDIENCE = "mcp-platform";

    private AuthErrorCode() {
    }
}
