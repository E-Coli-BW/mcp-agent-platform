# Login & Registration System — Full-Stack Design

> Design doc for adding UI + backend login/registration to the MCP Platform.
> Covers: permission model, async registration via MQ, cache strategy,
> and the consistency challenges when DB + Redis + Kafka interact.

---

## 1. Current State & What We're Adding

### Already built (auth-service :8090)
- ✅ `POST /auth/signup` — sync user registration (bcrypt + DB)
- ✅ `POST /auth/login` — user login → RS256 JWT
- ✅ `POST /auth/token` — M2M client_credentials grant
- ✅ `GET /auth/jwks` — public key for JWT verification
- ✅ Policy model: `(actor, audience, tenant) → permissions`
- ✅ Brute-force protection (5 attempts → 5min lockout, in-memory)
- ✅ BCrypt password hashing, account enable/disable

### Adding
- 🆕 **Frontend**: React login/signup pages
- 🆕 **Async registration**: Kafka-based event pipeline (email verification, welcome email, analytics)
- 🆕 **Session/token cache**: Redis for token blacklist + rate limiting
- 🆕 **RBAC upgrade**: Role hierarchy with fine-grained permissions
- 🆕 **Refresh tokens**: Secure token rotation

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        React Frontend                            │
│  /login  /signup  /dashboard  /settings                          │
└──────┬───────────────┬───────────────────────────────────────────┘
       │               │
       ▼               ▼
┌──────────────────────────────────────────────────────────────────┐
│                    API Gateway (port 8080)                        │
│  Rate limit (IP-based) │ CORS │ JWT validation via JWKS          │
└──────┬───────────────┬──────────────────┬───────────────────────┘
       │               │                  │
       ▼               ▼                  ▼
  Auth Service    Memory Server     Agent Server
   (:8090)          (:8180)          (:8580)
       │
       ├── POST /auth/signup ──→ DB (sync: create user)
       │                    ──→ Kafka: user.registered (async)
       │                              │
       │                    ┌─────────┴──────────┐
       │                    ▼                    ▼
       │              Email Service      Analytics Service
       │              (send verify)      (track signup)
       │
       ├── POST /auth/login ──→ DB (verify password)
       │                    ──→ Redis (cache session, rate limit)
       │                    ──→ JWT (RS256 access + refresh token)
       │
       ├── POST /auth/refresh ──→ Redis (validate refresh token)
       │                       ──→ JWT (rotate tokens)
       │
       └── POST /auth/logout ──→ Redis (blacklist access token)
```

---

## 3. Permission Model (RBAC + Policy)

### 3.1 Role hierarchy

```
SUPER_ADMIN
  └── TENANT_ADMIN
       └── USER
            └── VIEWER

Permissions per role:
┌──────────────┬────────────────────────────────────────────────────┐
│ Role         │ Permissions                                        │
├──────────────┼────────────────────────────────────────────────────┤
│ SUPER_ADMIN  │ * (all)                                            │
│ TENANT_ADMIN │ USER_MANAGE, MEMORY_*, CHAT, SETTINGS, AUDIT_READ  │
│ USER         │ MEMORY_READ, MEMORY_WRITE, CHAT, SETTINGS_SELF     │
│ VIEWER       │ MEMORY_READ, CHAT_READ                             │
└──────────────┴────────────────────────────────────────────────────┘
```

### 3.2 Schema changes

```sql
-- V5: Add roles and user_roles tables
CREATE TABLE roles (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(50) NOT NULL UNIQUE,   -- 'SUPER_ADMIN', 'USER', etc.
    description VARCHAR(255),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE user_roles (
    user_id  BIGINT REFERENCES auth_users(id) ON DELETE CASCADE,
    role_id  INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE role_permissions (
    role_id     INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    permission  VARCHAR(50) NOT NULL,          -- 'MEMORY_READ', 'CHAT', etc.
    PRIMARY KEY (role_id, permission)
);

-- V6: Add refresh_tokens table
CREATE TABLE refresh_tokens (
    id          VARCHAR(36) PRIMARY KEY,       -- UUID
    user_id     BIGINT NOT NULL REFERENCES auth_users(id),
    token_hash  VARCHAR(64) NOT NULL,          -- SHA-256 of refresh token
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    device_info VARCHAR(255)                   -- "Chrome/macOS" for session management
);
CREATE INDEX idx_refresh_user ON refresh_tokens(user_id, revoked);
```

### 3.3 JWT claims (upgraded)

```json
{
  "iss": "mcp-auth-service",
  "sub": "alice",
  "aud": ["agent-server", "memory-server"],
  "tenant_id": "tenant-1",
  "sub_type": "USER",
  "roles": ["ROLE_USER"],
  "permissions": ["MEMORY_READ", "MEMORY_WRITE", "CHAT"],
  "iat": 1716278400,
  "exp": 1716282000,
  "jti": "uuid-xxx"
}
```

### 3.4 Permission check flow

```
Request → JwtAuthFilter → extract roles + permissions from JWT
                        → SecurityContext.setAuthentication(...)
                        → @PreAuthorize("hasAuthority('MEMORY_WRITE')")
                        → TenantContext.set(jwt.tenant_id)
                        → Hibernate filter: WHERE tenant_id = :tenantId
```

**No DB lookup on every request** — permissions are embedded in the JWT.
Trade-off: permission changes take effect only after token refresh (max 15min).

---

## 4. Async Registration via Kafka

### 4.1 Why async?

Synchronous registration does: validate → hash password → DB insert → return.
But production registration also needs:
1. Send verification email (500ms-3s, may fail)
2. Create default tenant resources (workspace, quota)
3. Send welcome notification
4. Track analytics event
5. Notify admin dashboard

Making all of this synchronous = **2-5s signup latency** + any failure blocks the user.

### 4.2 Design: sync core + async side effects

```
POST /auth/signup
  │
  ├─ SYNC (must succeed for signup to complete):
  │   1. Validate input
  │   2. Check duplicate username (DB query)
  │   3. BCrypt hash password
  │   4. INSERT into auth_users (status='PENDING_VERIFICATION')
  │   5. INSERT into user_roles (default: USER)
  │   6. Generate email verification token → Redis (TTL 24h)
  │   7. Return 201 Created {userId, message: "Check your email"}
  │
  └─ ASYNC (published to Kafka, processed by consumers):
      Topic: user.events
      Event: {
        "type": "USER_REGISTERED",
        "userId": 42,
        "username": "alice",
        "email": "alice@example.com",
        "tenantId": "tenant-1",
        "timestamp": "2026-05-21T10:00:00Z"
      }
      │
      ├─→ EmailConsumer: send verification email
      ├─→ QuotaConsumer: create tenant_quota entry (10K limit)
      ├─→ AnalyticsConsumer: track signup in analytics DB
      └─→ NotificationConsumer: notify admin Slack/webhook
```

### 4.3 Kafka topic design

```
Topics:
  user.events           — lifecycle events (registered, verified, disabled, deleted)
  user.email.commands   — email sending commands (verification, welcome, password reset)
  user.audit            — audit trail (login, logout, password change)

Partitioning:
  Key = tenantId (ensures ordering per tenant)
  Partitions = 6 (matches expected consumer group size)

Consumer groups:
  email-service         — sends emails
  quota-service         — manages tenant quotas
  analytics-service     — ingests events
  audit-service         — writes audit log
```

### 4.4 Failure handling

| Failure | Impact | Mitigation |
|---|---|---|
| Kafka down during signup | User is created but side effects don't fire | **Outbox pattern**: write event to `outbox` table in same transaction as user insert. Background poller publishes to Kafka when it recovers. |
| Email service crashes | User registered but can't verify | Consumer retries with backoff. Dead letter queue after 3 failures. Manual re-send endpoint. |
| Duplicate event | Analytics counts double | Consumer idempotency: `INSERT ... ON CONFLICT (event_id) DO NOTHING` |

### 4.5 Outbox pattern (Transactional Outbox)

```sql
-- Same transaction as user INSERT
CREATE TABLE outbox_events (
    id          VARCHAR(36) PRIMARY KEY,
    topic       VARCHAR(100) NOT NULL,
    event_key   VARCHAR(100),           -- partition key
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_outbox_pending ON outbox_events(published, created_at) WHERE NOT published;

-- Background poller (every 1s):
-- SELECT * FROM outbox_events WHERE published = FALSE ORDER BY created_at LIMIT 100
-- → publish to Kafka
-- → UPDATE outbox_events SET published = TRUE WHERE id IN (...)
```

**Why outbox vs. direct Kafka publish?**
- Direct publish: if Kafka is down, signup fails (bad UX)
- Direct publish: if Kafka publish succeeds but DB commit fails, ghost event (data inconsistency)
- Outbox: DB commit is atomic with event creation → **exactly-once** semantics

---

## 5. Cache Strategy (Redis)

### 5.1 What we cache and why

| Cache Key | TTL | Purpose | Invalidation |
|---|---|---|---|
| `token:blacklist:{jti}` | = token's remaining TTL | Logout / forced revocation | Auto-expires |
| `user:session:{userId}` | 15min | Active session count, last activity | On logout, on token refresh |
| `rate:login:{ip}` | 15min, sliding | IP-based login rate limit | Auto-expires |
| `rate:login:{username}` | 5min | Per-user brute-force protection | On successful login |
| `email:verify:{token}` | 24h | Email verification token → userId | On verification, auto-expires |
| `user:permissions:{userId}` | 5min | Cached role+permissions lookup | On role change (pub/sub) |

### 5.2 Token blacklist (logout)

```
POST /auth/logout
  Authorization: Bearer <access_token>

  1. Parse JWT → extract jti, exp
  2. Calculate remaining TTL: exp - now()
  3. SET token:blacklist:{jti} "1" EX {remaining_ttl}
  4. DELETE refresh_tokens WHERE user_id = ? AND revoked = FALSE
  5. Return 200 OK

JwtAuthFilter (every request):
  1. Parse JWT
  2. EXISTS token:blacklist:{jti}  ← O(1) Redis lookup
  3. If blacklisted → 401 Unauthorized
```

### 5.3 Distributed rate limiting

```java
// Redis Lua script for sliding window rate limit
// Key: rate:login:{ip}
// Limit: 20 attempts per 15 minutes
String RATE_LIMIT_SCRIPT = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count < limit then
        redis.call('ZADD', key, now, now .. math.random())
        redis.call('EXPIRE', key, window)
        return 1
    end
    return 0
""";
```

**Why Redis instead of in-memory (current)?**
- Current `ConcurrentHashMap` brute-force protection is per-instance
- With 10+ instances, attacker can spread attempts across instances
- Redis = single source of truth for rate limiting across all instances

---

## 6. DB + Redis + Kafka — Consistency Challenges

### 6.1 The dual-write problem

```
❌ WRONG: Write to DB, then write to Redis, then publish to Kafka
  → If Redis fails: DB has data, cache is stale
  → If Kafka fails: DB has data, downstream never notified
  → If service crashes between DB and Redis: inconsistent

✅ RIGHT: Outbox pattern + cache-aside
  1. DB transaction: INSERT user + INSERT outbox_event (atomic)
  2. Return success to client
  3. Background: poll outbox → publish to Kafka → mark published
  4. Cache: invalidate on read-miss (cache-aside), not on write
```

### 6.2 Cache-aside vs. write-through

We use **cache-aside** (read-through) for user permissions:
```
GET user permissions:
  1. Check Redis: GET user:permissions:{userId}
  2. If HIT → return cached permissions
  3. If MISS → query DB → SET user:permissions:{userId} EX 300 → return

UPDATE user role:
  1. Update DB
  2. DEL user:permissions:{userId}  ← invalidate, don't write-through
  3. Publish: user.events {type: "ROLE_CHANGED", userId, newRoles}
  4. Other instances subscribe → DEL their local cache (if any)
```

**Why not write-through?**
- Write-through: DB write + cache write = two failure points
- If cache write fails, stale data served until TTL
- Cache-aside: worst case = one extra DB read on next request (self-healing)

### 6.3 Redis failure scenarios

| Scenario | Impact | Mitigation |
|---|---|---|
| Redis down (read) | Can't check token blacklist | **Degrade to allow** — accept JWT if Redis unavailable (prefer availability over revocation) OR **degrade to deny** for high-security environments |
| Redis down (write) | Can't cache session | Log + continue — login still works, just slower |
| Redis data loss (restart without persistence) | All blacklisted tokens are "un-blacklisted" | Use Redis AOF persistence + sentinels; short access token TTL (15min) limits exposure |
| Redis + DB clock skew | TTL calculations wrong | Use Redis `TIME` command, not local clock |

### 6.4 Kafka failure scenarios

| Scenario | Impact | Mitigation |
|---|---|---|
| Kafka down during signup | Side effects delayed | Outbox pattern → events queued in DB, published when Kafka recovers |
| Consumer crash | Events accumulate | Consumer group rebalance → another consumer picks up |
| Duplicate delivery | Email sent twice | Consumer idempotency key (event_id in DB) |
| Event ordering | Role change before signup event | Partition by tenantId → ordered within tenant |

---

## 7. Frontend Design

### 7.1 Existing frontend (platform/agent-server/app/static/index.html)

The project already has a **Coding Agent IDE** — a single-page app with login, signup, Monaco editor, file tree, and SSE streaming chat. It's a vanilla HTML/JS SPA (no React/build step needed).

```
Current features:
├── Login overlay         — username + password form, error handling
├── Signup overlay        — username + password + email + tenant_id, auto-login after signup
├── Header bar            — workspace path, open button, status, current user, logout
├── Sidebar               — file tree explorer with directory expand/collapse
├── Editor                — Monaco editor with tabs, syntax highlighting, multi-file
├── Chat panel            — SSE streaming, tool call chips (🔍 read / 📝 write)
├── Auth                  — JWT in localStorage, 401 → auto-logout
└── API integration       — /auth/login, /auth/signup, /v1/chat/completions (streaming)
```

**Current auth flow (already working)**:
```javascript
// Login: POST /auth/login → store token in localStorage
authToken = data.access_token;
localStorage.setItem('auth_token', authToken);

// Every API call: Authorization: Bearer ${authToken}
fetch(`${API}/v1/chat/completions`, {
  headers: { 'Authorization': `Bearer ${authToken}` },
  ...
});

// 401 response → handleLogout() → show login overlay
if (resp.status === 401) { handleLogout(); return; }
```

### 7.2 What to upgrade (in the existing index.html)

The current auth works but has security gaps and missing features:

| Current | Problem | Upgrade |
|---|---|---|
| Token in `localStorage` | XSS can steal it | Move access token to JS variable; use httpOnly cookie for refresh token |
| No refresh token | User re-logs in after 1h | Add `/auth/refresh` with silent token rotation |
| No role display | User doesn't know permissions | Show role badge next to username in header |
| No settings page | Can't change password | Add inline settings panel in sidebar |
| No email verification | Fake signups | Add verification banner + resend button |
| Signup is synchronous | Slow if email/analytics added | Backend upgrade to Kafka outbox (§4) |

### 7.3 Minimal frontend changes needed

```javascript
// 1. Move token from localStorage to memory (XSS protection)
let authToken = null;  // was: localStorage.getItem('auth_token')

// 2. Add auto-refresh interceptor
async function authFetch(url, opts = {}) {
  opts.headers = { ...opts.headers, 'Authorization': `Bearer ${authToken}` };
  let resp = await fetch(url, opts);
  if (resp.status === 401 && authToken) {
    // Try refresh
    const refreshResp = await fetch('/auth/refresh', {
      method: 'POST', credentials: 'include'  // httpOnly cookie
    });
    if (refreshResp.ok) {
      authToken = (await refreshResp.json()).access_token;
      opts.headers['Authorization'] = `Bearer ${authToken}`;
      resp = await fetch(url, opts);
    } else {
      handleLogout();
    }
  }
  return resp;
}

// 3. Show user role in header
document.getElementById('current-user').textContent =
  `${currentUser} (${currentRole})`;

// 4. Add logout → POST /auth/logout to blacklist token
async function handleLogout() {
  if (authToken) {
    await fetch('/auth/logout', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${authToken}` }
    }).catch(() => {});
  }
  authToken = null;
  showLogin();
}
```

### 7.4 Security best practices

| Concern | Solution |
|---|---|
| XSS → token theft | Access token in JS variable (not localStorage), short TTL (15min) |
| CSRF | Refresh token in httpOnly cookie + SameSite=Strict |
| Token rotation | Each refresh issues new access + refresh token, old refresh invalidated |
| Brute force | IP rate limit (Redis) + per-user lockout (Redis) |
| Password reset | Time-limited token via email, not security questions |

---

## 8. Implementation Priority

| Phase | What | Effort | Interview Value |
|---|---|---|---|
| **Phase 1** | RBAC tables + role hierarchy + JWT claims upgrade | 1 day | ⭐⭐⭐ Shows security depth |
| **Phase 2** | Refresh tokens + token blacklist (Redis) | 1 day | ⭐⭐⭐ Shows cache + security |
| **Phase 3** | Kafka outbox + async registration events | 1 day | ⭐⭐⭐⭐⭐ Shows distributed systems |
| **Phase 4** | React login/signup UI + API integration | 1 day | ⭐⭐ Shows full-stack |
| **Phase 5** | Email verification flow + password reset | 0.5 day | ⭐⭐ Shows async workflow |
| **Phase 6** | Admin user management page | 0.5 day | ⭐ Nice-to-have |

---

## 9. Interview Talking Points

1. **"Why Kafka for registration instead of direct calls?"** — Decoupling. The core signup (DB insert) must be fast and reliable. Email, analytics, quota setup can be delayed. If the email service is down, the user is still registered. Outbox pattern ensures no events are lost even if Kafka is down.

2. **"How do you handle the dual-write problem?"** — Transactional outbox. User insert + event insert happen in one DB transaction. A background poller publishes to Kafka. This gives us exactly-once semantics without distributed transactions.

3. **"Why cache-aside instead of write-through for permissions?"** — Write-through has two failure points (DB + Redis). If Redis write fails after DB succeeds, we serve stale data until TTL. Cache-aside is self-healing: worst case is one extra DB read. For permissions (changes are rare, reads are frequent), cache-aside is the right choice.

4. **"How does the rate limiter work across multiple instances?"** — Redis sorted set with sliding window. Each login attempt is a score (timestamp). We `ZREMRANGEBYSCORE` expired entries, `ZCARD` to count recent ones. All in a Lua script for atomicity. This replaces the current in-memory `ConcurrentHashMap` which is per-instance.

5. **"What happens if Redis is down during logout?"** — Design decision: we chose **degrade to allow** (accept JWTs even if blacklist is unavailable). Access tokens have 15min TTL, so max exposure is 15 minutes. For high-security: switch to **degrade to deny** but accept higher false-rejection rate.

---

## 10. Future: Frontend Modernization & IDE Features

> **Priority**: After login/registration is fully implemented.
> **Note**: Current vanilla HTML/JS SPA works well for MVP. These are requirements for the next iteration.

### 10.1 Migrate to modern stack

**Why**: The single `index.html` (~500 lines) is becoming hard to maintain. Component-based architecture enables faster iteration.

```
Recommended stack:
  React 19 + TypeScript + Vite + Tailwind CSS
  (same as agent-learning frontend — reuse patterns)

Migration path:
  1. Extract auth logic → auth.ts module
  2. Extract Monaco editor → <CodeEditor /> component
  3. Extract file tree → <FileTree /> component
  4. Extract chat panel → <ChatPanel /> component
  5. Wrap in react-router with /login, /ide, /settings routes
  6. Keep the same API contract — no backend changes needed
```

### 10.2 Native file system integration

**Requirement**: Users should pick workspace paths via OS file dialog, not copy-paste.

```
Option A: Electron wrapper (desktop app)
  - dialog.showOpenDialog({ properties: ['openDirectory'] })
  - Full native FS access, no sandboxing
  - Can read env vars: process.env.HOME, process.env.PWD
  - Tradeoff: requires packaging & distribution

Option B: File System Access API (Chrome 86+, web-only)
  - window.showDirectoryPicker()
  - Returns FileSystemDirectoryHandle with read/write
  - Works in Chrome/Edge, not Firefox/Safari
  - Tradeoff: browser-only, permission prompts

Option C: VS Code extension (webview)
  - vscode.workspace.workspaceFolders
  - Full VS Code API access (terminals, editors, diagnostics)
  - Best DX for developers who already use VS Code
  - Tradeoff: coupled to VS Code

Recommendation: Start with Option B (web, zero install), add Option A
later for power users who want native experience.
```

### 10.3 Live coding suggestions (inline completion)

**Requirement**: As user types, show inline ghost-text suggestions (like GitHub Copilot), not just chat-then-accept.

```
Architecture:
  Keystroke → debounce (300ms) → extract context → POST /v1/completions → ghost text

  ┌─────────────────────────────────────────────────┐
  │  Monaco Editor                                   │
  │                                                  │
  │  function fibonacci(n) {                         │
  │    if (n <= 1) return n;                         │
  │    return fibonacci█(n-1) + fibonacci(n-2);      │
  │           ↑ ghost text (Tab to accept)            │
  └─────────────────────────────────────────────────┘

Key components:
  1. Context extractor:
     - Current file content + cursor position
     - Open tabs (neighboring files for cross-file context)
     - File tree structure (project awareness)
     - Language detection from file extension

  2. Debounced request:
     - Trigger: 300ms after last keystroke (not every keystroke)
     - Cancel: if user types again before response arrives
     - AbortController to cancel in-flight requests

  3. Ghost text rendering (Monaco API):
     - monaco.languages.registerInlineCompletionsProvider()
     - Return InlineCompletion[] with insertText
     - Tab to accept, Esc to dismiss

  4. Backend: POST /v1/completions (FIM — Fill-in-the-Middle)
     {
       "model": "qwen2.5-coder:7b",
       "prompt": "<|fim_prefix|>...code before cursor...<|fim_suffix|>...code after cursor...<|fim_middle|>",
       "max_tokens": 128,
       "temperature": 0.2,
       "stop": ["\n\n", "<|fim_pad|>"]
     }

  5. Performance budget:
     - Request latency: < 500ms (user perceives lag > 1s)
     - Context window: prefix 2K tokens + suffix 1K tokens
     - Cancel stale requests aggressively
     - Local cache: LRU cache of (file_hash, cursor_pos) → suggestion
```

### 10.4 Implementation phases for IDE upgrade

| Phase | What | Backend Change? |
|---|---|---|
| **A** | React migration (same features, component-based) | None |
| **B** | OS file picker (File System Access API) | None |
| **C** | FIM completion endpoint (`/v1/completions`) | New endpoint in agent-server |
| **D** | Monaco inline completion provider | None (frontend only) |
| **E** | Electron wrapper for native FS + env vars | None |
