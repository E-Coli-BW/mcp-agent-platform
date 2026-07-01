package com.example.auth.config;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Configuration;

/**
 * Auth token configuration — externalizes TTL values per Alibaba Constant Definition #1.
 *
 * <p>Replaces hardcoded 3600 in TokenService, UserService, RefreshTokenService.</p>
 */
@Configuration
@ConfigurationProperties(prefix = "auth.token")
public class AuthTokenProperties {

    /** Access token TTL in seconds. Default: 3600 (1 hour). */
    private long accessTtlSeconds = 3600;

    /** Refresh token TTL in days. Default: 30. */
    private long refreshTtlDays = 30;

    /** Max failed login attempts before lockout. */
    private int maxFailedAttempts = 5;

    /** Lockout duration in milliseconds. Default: 300000 (5 minutes). */
    private long lockoutDurationMs = 300_000;

    public long getAccessTtlSeconds() { return accessTtlSeconds; }
    public void setAccessTtlSeconds(long accessTtlSeconds) { this.accessTtlSeconds = accessTtlSeconds; }
    public long getRefreshTtlDays() { return refreshTtlDays; }
    public void setRefreshTtlDays(long refreshTtlDays) { this.refreshTtlDays = refreshTtlDays; }
    public int getMaxFailedAttempts() { return maxFailedAttempts; }
    public void setMaxFailedAttempts(int maxFailedAttempts) { this.maxFailedAttempts = maxFailedAttempts; }
    public long getLockoutDurationMs() { return lockoutDurationMs; }
    public void setLockoutDurationMs(long lockoutDurationMs) { this.lockoutDurationMs = lockoutDurationMs; }
}
