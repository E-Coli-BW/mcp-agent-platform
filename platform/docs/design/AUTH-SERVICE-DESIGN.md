# Auth Service — Centralized Authentication & Authorization

## 1. Motivation

### Current State (Ad-hoc JWT)
Every service implements its own JWT generation and verification:
- **Python agent**: `jwt_auth.py` middleware + `mcp_client.py` token generation (HMAC by hand)
- **Java backends**: `JwtAuthFilter.java` + `McpSecurityConfigBase.java` (jjwt library)
- **Shared secret**: Same `AGENT_JWT_SECRET` env var copied to every service

### Problems
1. **Secret sprawl** — one leaked secret compromises ALL services
2. **No revocation** — once a JWT is issued, it's valid until expiry
3. **No audience validation** — a token for memory-server works on codeexec-server
4. **No user identity** — only service-to-service auth; no path to user login
5. **Duplicated logic** — JWT signing/verification in Python AND Java
6. **No key rotation** — changing the secret requires restarting every service

### Desired State
A centralized Auth Service that:
- Issues tokens (M2M and user login)
- Provides public keys for verification (JWKS)
- Manages tenants, API keys, roles, and permissions
- Enables key rotation without restarting other services

---

## 2. Architecture

### 2.1 System Topology

```
                          ┌─────────────────────┐
                          │    Auth Service      │
                          │    (Spring Boot)     │
                          │                      │
                          │  POST /auth/token    │ ← M2M: client_credentials
                          │  POST /auth/login    │ ← User: username/password (future)
                          │  GET  /auth/jwks     │ ← Public key for verification
                          │  POST /auth/introspect│ ← Token introspection (optional)
                          │                      │
                          │  Stores:             │
                          │  - RSA key pair       │
                          │  - Registered clients │
                          │  - Tenant configs     │
                          └──────────┬───────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
         ┌────▼─────┐         ┌─────▼──────┐        ┌──────▼─────┐
         │  Python   │         │   Memory   │        │  FileSearch│
         │  Agent    │         │   Server   │        │   Server   │
         │          │         │            │        │            │
         │ 1. Call   │         │ Verifies   │        │ Verifies   │
         │ /auth/    │         │ JWT with   │        │ JWT with   │
         │ token     │         │ public key │        │ public key │
         │ 2. Attach │         │ from JWKS  │        │ from JWKS  │
         │ JWT to    │─────────▶            │        │            │
         │ requests  │         │ Checks:    │        │ Checks:    │
         └───────────┘         │ - signature│        │ - signature│
                               │ - expiry   │        │ - expiry   │
                               │ - audience │        │ - audience │
                               │ - roles    │        │ - roles    │
                               └────────────┘        └────────────┘
```

### 2.2 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Signing algorithm | **RS256 (asymmetric)** | Only auth service holds private key. All others verify with public key — no shared secret. |
| Key distribution | **JWKS endpoint** (`/auth/jwks`) | Standard RFC 7517. Clients cache + auto-refresh. Enables key rotation. |
| Token format | **Standard JWT** (RFC 7519) | Compatible with any OAuth2 library in any language. |
| Grant types | **`client_credentials`** (M2M) + **`password`** (future user login) | Covers both service-to-service and user-facing auth. |
| Storage | **H2 (dev) / PostgreSQL (prod)** | Reuse existing infrastructure. |
| Framework | **Spring Boot + Spring Authorization Server** | Production-grade, standards-compliant, same tech stack as other Java services. |

---

## 3. API Design

### 3.1 Token Endpoint — M2M (Service-to-Service)

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=agent-server
&client_secret=<api-secret>
&audience=memory-server
&tenant_id=tenant-123
```

Response:
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### 3.2 Token Claims

```json
{
  "iss": "mcp-auth-service",
  "sub": "agent-server",
  "aud": "memory-server",
  "tenant_id": "tenant-123",
  "roles": ["SERVICE", "MEMORY_WRITE", "MEMORY_READ"],
  "iat": 1716220800,
  "exp": 1716224400,
  "jti": "unique-token-id"
}
```

| Claim | Purpose |
|-------|---------|
| `iss` | Identifies the auth service (for multi-auth-service setups) |
| `sub` | The client/user requesting access |
| `aud` | Which service this token is valid for (prevents reuse) |
| `tenant_id` | Multi-tenant context — used by RLS, data partitioning |
| `roles` | Fine-grained permissions for the target service |
| `jti` | Unique ID for token revocation |

### 3.3 JWKS Endpoint — Public Key Distribution

```
GET /auth/jwks

{
  "keys": [
    {
      "kty": "RSA",
      "kid": "key-2026-05",
      "use": "sig",
      "alg": "RS256",
      "n": "...",
      "e": "AQAB"
    }
  ]
}
```

Services fetch this on startup and cache it. On signature verification failure, they re-fetch (handles key rotation).

### 3.4 User Login (Future Phase)

```
POST /auth/login
Content-Type: application/json

{
  "username": "alice@tenant-123.com",
  "password": "...",
  "tenant_id": "tenant-123"
}

→ JWT with roles: ["USER", "MEMORY_READ"]
```

### 3.5 Token Introspection (Optional)

```
POST /auth/introspect
Authorization: Bearer <service-token>

{
  "token": "<token-to-check>"
}

→ { "active": true, "sub": "agent-server", "tenant_id": "tenant-123", ... }
```

---

## 4. Data Model

### 4.1 Registered Clients (services + API keys)

```sql
CREATE TABLE auth_clients (
    client_id       VARCHAR(100) PRIMARY KEY,
    client_secret   VARCHAR(255) NOT NULL,  -- bcrypt hashed
    client_name     VARCHAR(255),
    grant_types     VARCHAR(500),           -- "client_credentials"
    scopes          VARCHAR(500),           -- "memory:read,memory:write,code:exec"
    tenant_id       VARCHAR(100),           -- NULL = cross-tenant service
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW()
);
```

### 4.2 Pre-registered Clients (dev defaults)

| client_id | scopes | tenant_id | Description |
|-----------|--------|-----------|-------------|
| `agent-server` | `memory:*,filesearch:*,code:*` | NULL (cross-tenant) | Python agent → all backends |
| `web-frontend` | `chat:*` | per-user | Future: web UI → agent |
| `admin-cli` | `*` | NULL | Admin operations |

### 4.3 RSA Key Pair

Stored in `auth_keys` table or loaded from filesystem:

```sql
CREATE TABLE auth_keys (
    kid             VARCHAR(50) PRIMARY KEY,
    private_key     TEXT NOT NULL,           -- PEM-encoded, encrypted at rest
    public_key      TEXT NOT NULL,           -- PEM-encoded, served via JWKS
    algorithm       VARCHAR(10) DEFAULT 'RS256',
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    rotated_at      TIMESTAMP
);
```

---

## 5. Service Integration

### 5.1 Python Agent (Token Requester)

```python
class AuthClient:
    """Requests tokens from the centralized auth service."""

    def __init__(self, auth_url: str, client_id: str, client_secret: str):
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._cache: dict[str, tuple[str, float]] = {}  # audience → (token, expiry)

    async def get_token(self, audience: str, tenant_id: str) -> str:
        cache_key = f"{audience}:{tenant_id}"
        if cache_key in self._cache:
            token, exp = self._cache[cache_key]
            if time.time() < exp - 60:  # refresh 60s before expiry
                return token

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.auth_url}/auth/token", data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "audience": audience,
                "tenant_id": tenant_id,
            })
            data = resp.json()
            token = data["access_token"]
            self._cache[cache_key] = (token, time.time() + data["expires_in"])
            return token
```

### 5.2 Java Backends (Token Verifier)

Replace `JwtAuthFilter` with JWKS-based verification:

```java
@Bean
public SecurityFilterChain filterChain(HttpSecurity http) {
    return http
        .oauth2ResourceServer(oauth2 -> oauth2
            .jwt(jwt -> jwt
                .jwkSetUri("http://auth-service:8090/auth/jwks")
                .jwtAuthenticationConverter(jwtConverter())))
        .build();
}

// Extract tenant_id and roles from JWT claims
private JwtAuthenticationConverter jwtConverter() {
    var converter = new JwtAuthenticationConverter();
    converter.setJwtGrantedAuthoritiesConverter(jwt -> {
        var roles = jwt.getClaimAsStringList("roles");
        return roles.stream()
            .map(SimpleGrantedAuthority::new)
            .collect(Collectors.toList());
    });
    return converter;
}
```

### 5.3 Migration Path

| Phase | What Changes | Backward Compatible? |
|-------|-------------|---------------------|
| **Phase 1** | Deploy auth service, register clients, generate RSA keys | ✅ Old JWT still works |
| **Phase 2** | Python agent requests tokens from auth service instead of self-signing | ✅ Java still accepts both |
| **Phase 3** | Java backends switch to JWKS verification | ✅ Auth service signs RS256 |
| **Phase 4** | Remove old shared-secret JWT code | ❌ Breaking — all services must be updated |
| **Phase 5** | Add user login, RBAC, token revocation | ✅ New features only |

---

## 6. Security Considerations

### Key Rotation
1. Auth service generates new RSA key pair with new `kid`
2. Old key remains in JWKS for `token_expiry` duration (grace period)
3. New tokens are signed with new key
4. After grace period, old key is removed from JWKS

### Token Revocation
- Short-lived tokens (1 hour) minimize the revocation window
- For immediate revocation: auth service maintains a `revoked_jtis` set
- Services call `/auth/introspect` for high-security operations (optional)

### Audience Validation
- Token for `memory-server` is rejected by `filesearch-server`
- Prevents lateral movement if one service is compromised

### Tenant Isolation
- Token includes `tenant_id` — set by auth service, not by the caller
- Auth service validates that the client is allowed to access the requested tenant
- Cross-tenant service accounts (like `agent-server`) are explicitly registered

---

## 7. Deployment

### Docker Compose Addition

```yaml
auth-service:
  build: ./auth-service
  ports:
    - "8090:8090"
  environment:
    - SPRING_DATASOURCE_URL=jdbc:postgresql://postgres:5432/authdb
    - AUTH_RSA_KEY_PATH=/keys/auth-rsa.pem
  volumes:
    - auth-keys:/keys
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8090/auth/jwks"]
```

### Environment Variables (per service)

| Service | Old (shared secret) | New (auth service) |
|---------|--------------------|--------------------|
| Python Agent | `AGENT_JWT_SECRET=xxx` | `AUTH_SERVICE_URL=http://auth:8090`<br>`AUTH_CLIENT_ID=agent-server`<br>`AUTH_CLIENT_SECRET=xxx` |
| Java Memory | `mcp.security.jwt-secret=xxx` | `spring.security.oauth2.resourceserver.jwt.jwk-set-uri=http://auth:8090/auth/jwks` |

---

## 8. What This Enables (Future)

| Capability | How |
|-----------|-----|
| **User login** | `/auth/login` → JWT with `sub=user@email`, `roles=[USER]` |
| **Admin panel** | Role `ADMIN` → can manage tenants, view analytics |
| **API keys** | Third-party apps register as clients, get scoped tokens |
| **OAuth2 SSO** | Auth service becomes the authorization server for external integrations |
| **Audit trail** | All token issuance logged centrally |
| **Rate limiting by identity** | Token `sub` + `tenant_id` = rate limit key |

---

## 9. Implementation Estimate

| Component | Effort | Priority |
|-----------|--------|----------|
| Auth service (token endpoint + JWKS) | 2 days | P0 |
| Python AuthClient (replaces self-signed JWT) | 0.5 day | P0 |
| Java JWKS verification (replaces JwtAuthFilter) | 0.5 day | P0 |
| Client registration API | 1 day | P1 |
| User login endpoint | 1 day | P2 |
| Token revocation | 0.5 day | P2 |
| Admin UI | 2 days | P3 |

**Total MVP (M2M auth): ~3 days**

---

## 10. Tenant-Audience-Actor Policy Model

### 10.1 Problem

Current JWT validation only checks:
- Is the signature valid? (cryptographic)
- Is the token expired? (temporal)

It does NOT check:
- Is this actor allowed to access this tenant?
- Is this actor allowed to call this audience (target service)?
- What specific permissions does this actor have for this tenant+audience?

### 10.2 The Policy Model

A **policy** is a tuple: `(actor, audience, tenant, permissions)`

```
┌──────────────────────────────────────────────────────────┐
│                    Policy Table                           │
├──────────────┬──────────────────┬──────────┬─────────────┤
│ Actor        │ Audience         │ Tenant   │ Permissions │
├──────────────┼──────────────────┼──────────┼─────────────┤
│ agent-server │ memory-server    │ *        │ MEM_R,MEM_W │
│ agent-server │ codeexec-server  │ *        │ CODE_EXEC   │
│ agent-server │ filesearch-server│ *        │ FS_READ     │
│ web-frontend │ agent-server     │ tenant-1 │ CHAT        │
│ web-frontend │ agent-server     │ tenant-2 │ CHAT        │
│ alice@t1.com │ agent-server     │ tenant-1 │ CHAT,MEM_R  │
│ bob@t2.com   │ agent-server     │ tenant-2 │ CHAT        │
│ admin-cli    │ *                │ *        │ *           │
└──────────────┴──────────────────┴──────────┴─────────────┘
```

### 10.3 How It Works

#### Token Issuance (Auth Service)
```
1. Client sends: POST /auth/token
   { actor: "agent-server", audience: "memory-server", tenant: "tenant-1" }

2. Auth service:
   a. Authenticate actor (verify client_secret or user password)
   b. Policy lookup: SELECT * FROM policies
      WHERE actor = ? AND audience IN (?, '*') AND tenant IN (?, '*')
   c. If no policy → 403 Forbidden
   d. If policy found → issue JWT with permissions FROM THE POLICY
      (not from the client registration — from the specific policy match)
```

#### Token Verification (Backend Service)
```
1. Backend receives request with JWT
2. Verify: signature (via JWKS), expiry, issuer
3. Verify: aud == my service name (audience validation)
4. Extract: tenant_id, permissions
5. Enforce: tenant_id matches the data being accessed (RLS)
6. Enforce: permissions include the required permission for this operation
```

### 10.4 Data Model

```sql
CREATE TABLE auth_policies (
    id          BIGSERIAL PRIMARY KEY,
    actor       VARCHAR(255) NOT NULL,   -- client_id or user email
    actor_type  VARCHAR(20) NOT NULL,    -- 'SERVICE' or 'USER'
    audience    VARCHAR(255) NOT NULL,   -- target service or '*'
    tenant_id   VARCHAR(100) NOT NULL,   -- tenant or '*' (wildcard)
    permissions VARCHAR(500) NOT NULL,   -- comma-separated permissions
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT NOW(),

    UNIQUE(actor, audience, tenant_id)
);
```

### 10.5 Token Claims (Enhanced)

```json
{
  "iss": "mcp-auth-service",
  "sub": "agent-server",
  "sub_type": "SERVICE",
  "aud": "memory-server",
  "tenant_id": "tenant-1",
  "permissions": ["MEMORY_READ", "MEMORY_WRITE"],
  "policy_id": 42,
  "iat": 1716220800,
  "exp": 1716224400,
  "jti": "unique-token-id"
}
```

Key differences from current design:
- `sub_type`: distinguishes SERVICE from USER (same verification logic)
- `permissions`: specific to this actor+audience+tenant combo (not global scopes)
- `policy_id`: traceable back to the policy that granted access

### 10.6 Reuse for User Login

The same model works for users — a user is just another actor:

```
POST /auth/login
{ username: "alice@company.com", password: "...", tenant: "tenant-1" }

Auth service:
  1. Verify password (users table)
  2. Policy lookup: actor="alice@company.com", audience="agent-server", tenant="tenant-1"
  3. Issue JWT with sub="alice@company.com", sub_type="USER",
     permissions=["CHAT", "MEMORY_READ"]
```

The backend doesn't care if `sub_type` is SERVICE or USER — it checks:
- Is the JWT valid? (signature + expiry)
- Is `aud` correct? (audience)
- Does `permissions` include what's needed? (authorization)
- Is `tenant_id` the right tenant? (data isolation)

### 10.7 Permission Hierarchy

```
*              → superadmin (all permissions)
MEMORY_*       → all memory operations
MEMORY_READ    → read memory entries
MEMORY_WRITE   → create/update memory entries
MEMORY_DELETE  → delete memory entries
CODE_EXEC      → execute code
FS_READ        → read files
FS_WRITE       → write files
CHAT           → use the chat agent
ADMIN          → admin operations
```

### 10.8 Migration Path

| Phase | Change |
|-------|--------|
| Phase 3a | Add `auth_policies` table, seed with current client scopes |
| Phase 3b | Token issuance reads permissions from policy (not client.scopes) |
| Phase 3c | Java backends verify `aud` + `permissions` from JWT |
| Phase 4  | Add user login → same policy model, `sub_type=USER` |
| Phase 5  | Admin UI for policy management |
