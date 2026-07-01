/**
 * Auth module — token management, refresh, logout.
 *
 * Security design:
 * - Access token: in-memory only (never localStorage — XSS safe)
 * - Refresh token: in-memory only (XSS safe, lost on reload — user re-logins)
 * - On 401: auto-refresh silently, retry the request
 * - On refresh failure: redirect to /login
 */

let accessToken: string | null = null;
let refreshTokenValue: string | null = null; // in-memory only — not persisted (XSS safe)

export interface AuthState {
  user: string | null;
  tenant: string | null;
  roles: string[];
}

// Restore display-only state from localStorage
const state: AuthState = {
  user: localStorage.getItem('auth_user'),
  tenant: localStorage.getItem('auth_tenant'),
  roles: [],
};

// Notify listeners on auth state change
type AuthListener = (state: AuthState | null) => void;
const listeners: AuthListener[] = [];

export function onAuthChange(fn: AuthListener) {
  listeners.push(fn);
}

function notify(s: AuthState | null) {
  listeners.forEach((fn) => fn(s));
}

/** Parse roles from JWT payload (display only, no verification) */
function parseJwtRoles(token: string): string[] {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return payload.roles || [];
  } catch {
    return [];
  }
}

export function getAuthState(): AuthState {
  return { ...state };
}

export function isLoggedIn(): boolean {
  return !!accessToken || !!refreshTokenValue;
}

export function getAccessToken(): string | null {
  return accessToken;
}

/** Login: POST /auth/login → store tokens, parse roles */
export async function login(
  username: string,
  password: string,
): Promise<{ tenant: string; roles: string[] }> {
  const resp = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error_description || data.error || 'Login failed');
  }

  accessToken = data.access_token;
  refreshTokenValue = data.refresh_token;
  state.user = username;
  state.tenant = data.tenant_id;
  state.roles = parseJwtRoles(data.access_token);

  localStorage.setItem('auth_user', username);
  localStorage.setItem('auth_tenant', data.tenant_id);
  // SECURITY: Refresh token stored in memory only (not localStorage).
  // XSS cannot steal it. Trade-off: lost on page reload — user must re-login.
  // For session restore, the auth service should set an httpOnly cookie instead.
  // localStorage.setItem('auth_refresh', data.refresh_token);  // REMOVED — XSS risk

  notify(state);
  return { tenant: data.tenant_id, roles: state.roles };
}

/** Signup: POST /auth/signup → auto-login */
export async function signup(
  username: string,
  password: string,
  email: string,
  tenantId: string,
): Promise<void> {
  const resp = await fetch('/auth/signup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password, email, tenant_id: tenantId }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error || 'Signup failed');
  }
  // Auto-login after signup
  await login(username, password);
}

/** Silent refresh: exchange refresh token for new token pair */
export async function tryRefresh(): Promise<boolean> {
  if (!refreshTokenValue) return false;
  try {
    const resp = await fetch('/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshTokenValue }),
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    accessToken = data.access_token;
    refreshTokenValue = data.refresh_token;
    state.roles = parseJwtRoles(data.access_token);
    // SECURITY: refresh token stays in memory only (no localStorage)
    notify(state);
    return true;
  } catch {
    return false;
  }
}

/** Logout: blacklist token + revoke refresh tokens */
export async function logout(): Promise<void> {
  if (accessToken) {
    try {
      await fetch('/auth/logout', {
        method: 'POST',
        headers: { Authorization: `Bearer ${accessToken}` },
      });
    } catch {
      /* best effort */
    }
  }
  accessToken = null;
  refreshTokenValue = null;
  state.user = null;
  state.tenant = null;
  state.roles = [];
  localStorage.removeItem('auth_user');
  localStorage.removeItem('auth_tenant');
  localStorage.removeItem('auth_refresh');
  notify(null);
}

/**
 * Fetch wrapper with auto-refresh on 401.
 * Use this for all authenticated API calls.
 */
export async function authFetch(
  url: string,
  opts: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(opts.headers);
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);

  let resp = await fetch(url, { ...opts, headers });

  if (resp.status === 401 && refreshTokenValue) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      headers.set('Authorization', `Bearer ${accessToken}`);
      resp = await fetch(url, { ...opts, headers });
    } else {
      await logout();
    }
  }
  return resp;
}
