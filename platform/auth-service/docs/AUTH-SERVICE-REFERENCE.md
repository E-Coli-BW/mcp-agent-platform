# Auth Service — Technical Reference

## Overview

The Auth Service is a centralized authentication and authorization server for the MCP platform.
It issues RS256-signed JWTs based on policy rules, and provides a JWKS endpoint for
all other services to verify tokens without shared secrets.

---

## 1. API Reference

### POST /auth/token — Issue JWT Token

Request a JWT token using OAuth2 `client_credentials` grant.

```
POST /auth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=agent-server
&client_secret=agent-secret
&audience=memory-server
&tenant_id=tenant-123
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `grant_type` | Yes | Must be `client_credentials` |
| `client_id` | Yes | Registered client ID |
| `client_secret` | Yes | Client secret (plaintext, verified against BCrypt hash) |
| `audience` | No | Target service (default: `mcp-platform`). Used for policy lookup + JWT `aud` claim |
| `tenant_id` | No | Tenant context. Used for policy lookup + JWT `tenant_id` claim |

**Success Response (200):**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6ImtleS1hYjEyMzQ1NiJ9...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

**Error Response (401):**
```json
{
  "error": "invalid_client",
  "error_description": "Invalid client_secret"
}
```

### GET /auth/jwks — Public Key (JWKS)

Returns the RSA public key in JWKS format for JWT signature verification.

```
GET /auth/jwks
```

**Response (200):**
```json
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "key-ab123456",
      "use": "sig",
      "alg": "RS256",
      "n": "0vx7agoebGcQSuu...",
      "e": "AQAB"
    }
  ]
}
```

### POST /auth/register — Register Client

Register a new service or API key.

```
POST /auth/register
Content-Type: application/json

{
  "client_id": "my-new-service",
  "client_secret": "my-secret",
  "client_name": "My New Service",
  "scopes": "data:read,data:write",
  "tenant_id": null
}
```

### GET /auth/health — Health Check

```
GET /auth/health → { "status": "UP", "service": "auth-service" }
```

---

## 2. JWT Token Structure

### Header
```json
{ "alg": "RS256", "kid": "key-ab123456" }
```

### Payload
```json
{
  "iss": "mcp-auth-service",
  "sub": "agent-server",
  "aud": "memory-server",
  "sub_type": "SERVICE",
  "tenant_id": "tenant-123",
  "permissions": ["MEMORY_READ", "MEMORY_WRITE", "MEMORY_DELETE"],
  "iat": 1716220800,
  "exp": 1716224400,
  "jti": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Claim | Type | Description |
|-------|------|-------------|
| `iss` | string | Issuer — always `mcp-auth-service` |
| `sub` | string | Subject — the client_id or user email |
| `aud` | string | Audience — the target service this token is valid for |
| `sub_type` | string | `SERVICE` or `USER` |
| `tenant_id` | string | Tenant context for multi-tenancy |
| `permissions` | string[] | Permissions from the matching policy |
| `iat` | number | Issued at (Unix epoch) |
| `exp` | number | Expiration (Unix epoch, default +1 hour) |
| `jti` | string | Unique token ID (for revocation) |

---

## 3. Database Schema

### auth_clients

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `client_id` | VARCHAR(100) | PK | Unique client identifier |
| `client_secret` | VARCHAR(255) | NOT NULL | BCrypt-hashed secret |
| `client_name` | VARCHAR(255) | | Human-readable name |
| `scopes` | VARCHAR(500) | | Legacy comma-separated scopes (fallback) |
| `tenant_id` | VARCHAR(100) | | NULL = cross-tenant service account |
| `enabled` | BOOLEAN | NOT NULL, DEFAULT TRUE | Soft disable |
| `created_at` | TIMESTAMP | DEFAULT NOW | |

### auth_policies

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | BIGSERIAL | PK | Auto-increment ID |
| `actor` | VARCHAR(255) | NOT NULL | client_id or user email |
| `actor_type` | VARCHAR(20) | NOT NULL | `SERVICE` or `USER` |
| `audience` | VARCHAR(255) | NOT NULL | Target service or `*` |
| `tenant_id` | VARCHAR(100) | NOT NULL | Tenant or `*` (wildcard) |
| `permissions` | VARCHAR(500) | NOT NULL | Comma-separated permissions |
| `enabled` | BOOLEAN | NOT NULL, DEFAULT TRUE | Soft disable |
| `created_at` | TIMESTAMP | DEFAULT NOW | |
| | | UNIQUE(actor, audience, tenant_id) | Prevents duplicate policies |

### ER Diagram

```
┌─────────────────────┐         ┌──────────────────────────┐
│    auth_clients      │         │     auth_policies         │
├─────────────────────┤         ├──────────────────────────┤
│ client_id (PK)      │◄────────│ actor (FK conceptual)    │
│ client_secret       │         │ actor_type               │
│ client_name         │         │ audience                 │
│ scopes (legacy)     │         │ tenant_id                │
│ tenant_id           │         │ permissions              │
│ enabled             │         │ enabled                  │
│ created_at          │         │ id (PK)                  │
└─────────────────────┘         └──────────────────────────┘

Lookup: SELECT * FROM auth_policies
  WHERE actor = ? AND (audience = ? OR audience = '*')
                  AND (tenant_id = ? OR tenant_id = '*')
                  AND enabled = TRUE
```

---

## 4. Sequence Diagrams

### 4.1 Service-to-Service (M2M) Authentication

```
Python Agent                Auth Service               Memory Server
     │                           │                           │
     │  POST /auth/token         │                           │
     │  {client_id, secret,      │                           │
     │   audience=memory-server, │                           │
     │   tenant=t1}              │                           │
     │──────────────────────────►│                           │
     │                           │                           │
     │                  1. Verify client_secret (BCrypt)     │
     │                  2. Policy lookup:                     │
     │                     actor=agent-server                │
     │                     audience=memory-server            │
     │                     tenant=t1 (or *)                  │
     │                  3. Best match → permissions          │
     │                  4. Sign JWT (RS256, private key)     │
     │                           │                           │
     │  { access_token: "ey..." }│                           │
     │◄──────────────────────────│                           │
     │                           │                           │
     │  POST /api/tools/memory_set                          │
     │  Authorization: Bearer ey...                         │
     │──────────────────────────────────────────────────────►│
     │                           │                           │
     │                           │  1. Extract JWT from header
     │                           │  2. Fetch public key from │
     │                           │     GET /auth/jwks        │
     │                           │     (cached 5 min)        │
     │                           │  3. Verify RS256 signature│
     │                           │  4. Check: aud == me?     │
     │                           │  5. Check: permissions?   │
     │                           │  6. Set TenantContext      │
     │                           │  7. Process request       │
     │                           │                           │
     │  { result: "..." }                                    │
     │◄──────────────────────────────────────────────────────│
```

### 4.2 Token Verification (Detail)

```
Incoming Request
     │
     ▼
┌─────────────────────┐
│  JwtAuthFilter      │
│                     │
│  1. Extract Bearer  │
│     token from      │
│     Authorization   │
│     header          │
│                     │
│  2. Try RS256       │──── GET /auth/jwks ───► Auth Service
│     (JWKS)          │◄─── RSA public key ────┘
│     │               │     (cached 5 min)
│     ├── Success ────┤
│     │               │
│     └── Fail ───────┤
│                     │
│  3. Try HMAC        │  (fallback for legacy tokens)
│     (shared secret) │
│     │               │
│     ├── Success ────┤
│     │               │
│     └── Fail ───────┤──► 401 Unauthorized
│                     │
│  4. Extract claims: │
│     - tenant_id     │──► TenantContext.set()
│     - permissions   │──► GrantedAuthorities
│     - sub_type      │
│                     │
│  5. Spring Security │
│     checks:         │
│     - ROLE_SERVICE? │
│     - hasAuthority? │
└─────────────────────┘
         │
         ▼
   Controller handles request
   (tenant-scoped data access)
```

### 4.3 Future: User Login Flow

```
Web Browser              Auth Service              Agent Server
     │                        │                          │
     │  POST /auth/login      │                          │
     │  {username, password,  │                          │
     │   tenant=t1}           │                          │
     │───────────────────────►│                          │
     │                        │                          │
     │               1. Verify password (users table)    │
     │               2. Policy lookup:                   │
     │                  actor=alice@t1.com               │
     │                  audience=agent-server            │
     │                  tenant=t1                        │
     │               3. permissions=[CHAT, MEMORY_READ]  │
     │               4. Sign JWT (sub_type=USER)         │
     │                        │                          │
     │  { access_token }      │                          │
     │◄───────────────────────│                          │
     │                        │                          │
     │  POST /v1/chat/completions                       │
     │  Authorization: Bearer ey...                     │
     │──────────────────────────────────────────────────►│
     │                        │                          │
     │                        │   Same JwtAuthFilter:    │
     │                        │   verify RS256 + extract │
     │                        │   permissions + tenant   │
     │                        │                          │
     │  { streaming response }                           │
     │◄──────────────────────────────────────────────────│
```

---

## 5. Policy Resolution Algorithm

```
Input: (actor, audience, tenant)

1. Query: SELECT * FROM auth_policies
   WHERE actor = :actor
     AND (audience = :audience OR audience = '*')
     AND (tenant_id = :tenant OR tenant_id = '*')
     AND enabled = TRUE

2. If results found:
   a. Prefer exact tenant match over wildcard
   b. Prefer exact audience match over wildcard
   c. Use permissions from best match

3. If no results (backward compatible):
   a. Fall back to auth_clients.scopes
   b. Convert scopes to permissions (e.g., "memory:*" → "MEMORY_ALL")

Priority order:
  (actor, audience, tenant)     → highest specificity
  (actor, audience, *)          → audience-specific, all tenants
  (actor, *, tenant)            → all audiences, tenant-specific
  (actor, *, *)                 → lowest specificity (superadmin)
```

---

## 6. Default Clients & Policies (Dev Mode)

### Clients
| client_id | secret | scopes | tenant |
|-----------|--------|--------|--------|
| `agent-server` | `agent-secret` | `memory:*,filesearch:*,code:*` | NULL (cross-tenant) |
| `web-frontend` | `web-secret` | `chat:*` | NULL |
| `admin-cli` | `admin-secret` | `*` | NULL |

### Policies
| actor | type | audience | tenant | permissions |
|-------|------|----------|--------|-------------|
| `agent-server` | SERVICE | `memory-server` | `*` | MEMORY_READ, MEMORY_WRITE, MEMORY_DELETE |
| `agent-server` | SERVICE | `filesearch-server` | `*` | FS_READ |
| `agent-server` | SERVICE | `codeexec-server` | `*` | CODE_EXEC |
| `admin-cli` | SERVICE | `*` | `*` | `*` (superadmin) |

---

## 7. Configuration

### Auth Service (`application.properties`)
```properties
server.port=8090
spring.datasource.url=jdbc:h2:mem:authdb    # dev: H2
# spring.datasource.url=jdbc:postgresql://localhost:5432/authdb  # prod: PostgreSQL
spring.jpa.hibernate.ddl-auto=create-drop   # dev only
```

### Backend Services (consumers)
```properties
# Enable JWKS verification (RS256 from auth service)
mcp.security.jwks-url=http://auth-service:8090/auth/jwks

# HMAC fallback (legacy, still works without auth service)
mcp.security.jwt-secret=your-shared-secret
```

### Python Agent
```bash
# Auth service integration
AUTH_SERVICE_URL=http://localhost:8090
AUTH_CLIENT_ID=agent-server
AUTH_CLIENT_SECRET=agent-secret

# Legacy fallback (if AUTH_SERVICE_URL not set)
AGENT_JWT_SECRET=your-shared-secret
```

---

## 8. Security Properties

| Property | Value |
|----------|-------|
| Signing algorithm | RS256 (2048-bit RSA) |
| Token lifetime | 1 hour |
| Key rotation | Generate new key pair, old key stays in JWKS for 1 hour grace period |
| Secret storage | BCrypt (cost factor 10) |
| Audience validation | JWT `aud` must match receiving service name |
| Tenant isolation | JWT `tenant_id` scoped by policy; RLS enforced at DB level |
| Revocation | Short-lived tokens; future: `jti` blacklist |
