package com.example.auth.model;

import jakarta.persistence.*;

/**
 * Registered client (service or API key).
 * Each client can request tokens via client_credentials grant.
 */
@Entity
@Table(name = "auth_clients")
public class AuthClient {

    @Id
    @Column(length = 100)
    private String clientId;

    @Column(nullable = false)
    private String clientSecret; // bcrypt hashed

    @Column(length = 255)
    private String clientName;

    @Column(length = 500)
    private String scopes; // comma-separated: "memory:read,memory:write,code:exec"

    @Column(length = 100)
    private String tenantId; // null = cross-tenant service account

    @Column(nullable = false)
    private boolean enabled = true;

    public AuthClient() {}

    public AuthClient(String clientId, String clientSecret, String clientName,
                      String scopes, String tenantId) {
        this.clientId = clientId;
        this.clientSecret = clientSecret;
        this.clientName = clientName;
        this.scopes = scopes;
        this.tenantId = tenantId;
    }

    public String getClientId() { return clientId; }
    public void setClientId(String clientId) { this.clientId = clientId; }
    public String getClientSecret() { return clientSecret; }
    public void setClientSecret(String clientSecret) { this.clientSecret = clientSecret; }
    public String getClientName() { return clientName; }
    public void setClientName(String clientName) { this.clientName = clientName; }
    public String getScopes() { return scopes; }
    public void setScopes(String scopes) { this.scopes = scopes; }
    public String getTenantId() { return tenantId; }
    public void setTenantId(String tenantId) { this.tenantId = tenantId; }
    public boolean isEnabled() { return enabled; }
    public void setEnabled(boolean enabled) { this.enabled = enabled; }
}
