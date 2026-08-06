# Coding Agent IDE — Frontend

React 19 + TypeScript + Vite + Tailwind CSS frontend for the MCP Coding Agent platform.

## Quick Start

```bash
cd platform/frontend
npm ci --workspaces=false
npm run dev                          # → http://localhost:3000
```

## Full End-to-End Testing

### Prerequisites

1. **Java 21** — for auth-service and memory-server
2. **Python 3.11+** — for agent-server
3. **Node.js 18+** — for this frontend
4. **Ollama** (optional) — for LLM responses: `ollama run qwen2.5:7b`
5. **Docker** (optional) — for Redis + PostgreSQL (services degrade gracefully without them)

### Step 1: Start auth-service (port 8090)

```bash
cd platform/auth-service
command mvn spring-boot:run -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo
```

Verify: `curl http://localhost:8090/auth/health` → `{"status":"UP"}`

### Step 2: Start agent-server (port 8580)

```bash
cd platform/agent-server
source .venv/bin/activate        # IMPORTANT: must use project venv, not system Python
.venv/bin/uvicorn app.main:app --port 8580 --reload
```

Verify: `curl http://localhost:8580/health` → `{"status":"ok"}`

### Step 3: Start frontend (port 3000)

```bash
cd platform/frontend
npm run dev
```

Open http://localhost:3000 in your browser.

### Step 4: Test the flow

1. **Sign up**: Click "Sign up" → enter username, password (8+ chars), tenant ID → Create Account
2. **Auto-login**: You'll be redirected to the IDE after signup
3. **Open workspace**: Click Browse to select an existing server folder, or type its path and click Go
4. **Browse files**: Click files in the sidebar to open in Monaco editor
5. **Chat**: Type a message in the chat panel → see streaming response
6. **Approve tools**: For gated operations, review the approval card and choose Approve or Reject before any side effect runs
7. **Logout**: Click Logout in header → token is blacklisted, redirected to login
8. **Session restore**: Refresh the page → auto-refreshes token, returns to IDE

### Quick test (auth only, no agent needed)

```bash
# 1. Start only auth-service (Step 1 above)
# 2. Start frontend (Step 3 above)
# 3. Open http://localhost:3000
# 4. Sign up → auto-login → see IDE page (chat won't work without agent-server)
# 5. Logout → verify redirect to login
# 6. Login again → verify session works
```

### Verify auth features

```bash
# Test signup via curl
curl -X POST http://localhost:8090/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"username":"test","password":"test1234","email":"test@example.com","tenant_id":"default"}'

# Test login via curl (returns access_token + refresh_token)
curl -X POST http://localhost:8090/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"test","password":"test1234"}'

# Test token refresh
curl -X POST http://localhost:8090/auth/refresh \
  -H 'Content-Type: application/json' \
  -d '{"refresh_token":"<refresh_token_from_login>"}'

# Test logout (blacklists token)
curl -X POST http://localhost:8090/auth/logout \
  -H 'Authorization: Bearer <access_token_from_login>'
```

## Architecture

```
src/
├── lib/
│   └── auth.ts              — Token management, authFetch, refresh, logout
├── components/
│   ├── AuthGuard.tsx         — Route guard (redirect to /login if unauthenticated)
│   ├── Layout.tsx            — App shell: header bar with user/role + logout
│   ├── FileTree.tsx          — Workspace tree + server-side directory browser
│   ├── CodeEditor.tsx        — Monaco editor with multi-tab support
│   ├── ChatPanel.tsx         — SSE chat, reasoning/errors, and approval controls
│   └── ToolBlock.tsx         — Collapsible tool invocation and result view
├── pages/
│   ├── LoginPage.tsx         — Login form with error handling + loading state
│   ├── SignupPage.tsx        — Registration form with auto-login
│   └── IdePage.tsx           — Resizable three-column IDE layout
├── App.tsx                   — Router: /login, /signup, /ide (protected)
└── main.tsx                  — Entry point
```

## API Proxy (dev)

Vite proxies API calls to backend services:

| Path | Target | Service |
|---|---|---|
| `/auth/*` | `http://localhost:8090` | auth-service (login, signup, refresh, logout, JWKS) |
| `/v1/*` | `http://localhost:8580` | agent-server (chat completions, SSE streaming) |
| `/api/*` | `http://localhost:8580` | agent-server (workspace, file tree, file content) |

## Security

- Access token: **in-memory only** (never localStorage — XSS-safe)
- Refresh token: localStorage (for session restore across page refreshes)
- Auto-refresh on 401 → silent token rotation
- JWT role parsing for display (e.g., "alice (user)")
- Logout: POST /auth/logout → blacklists access token in Redis + revokes refresh tokens in DB

## Build

```bash
npm run build      # TypeScript check + Vite production build → dist/
npm run preview    # Preview production build locally
```
