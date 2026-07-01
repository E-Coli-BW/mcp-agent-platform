-- Auth Service Schema — V1
-- Tables: auth_clients (registered services/apps), auth_policies (authorization rules)

-- ── Registered Clients ───────────────────────────────────────
-- Each client (service or API key) can request tokens via client_credentials grant.
CREATE TABLE auth_clients (
    client_id       VARCHAR(100) PRIMARY KEY,
    client_secret   VARCHAR(255) NOT NULL,          -- BCrypt hashed
    client_name     VARCHAR(255),
    scopes          VARCHAR(500),                   -- legacy: comma-separated scopes
    tenant_id       VARCHAR(100),                   -- NULL = cross-tenant service account
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Authorization Policies ───────────────────────────────────
-- Policy = (actor, audience, tenant) → permissions
-- Defines what an actor can do on which service for which tenant.
-- Wildcards: audience='*' (any service), tenant_id='*' (any tenant)
CREATE TABLE auth_policies (
    id              BIGSERIAL PRIMARY KEY,
    actor           VARCHAR(255) NOT NULL,          -- client_id or user email
    actor_type      VARCHAR(20) NOT NULL,           -- 'SERVICE' or 'USER'
    audience        VARCHAR(255) NOT NULL,          -- target service or '*'
    tenant_id       VARCHAR(100) NOT NULL,          -- tenant or '*' (wildcard)
    permissions     VARCHAR(500) NOT NULL,           -- comma-separated: 'MEMORY_READ,MEMORY_WRITE'
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_policy UNIQUE (actor, audience, tenant_id)
);

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX idx_policies_actor ON auth_policies (actor);
CREATE INDEX idx_policies_lookup ON auth_policies (actor, audience, tenant_id) WHERE enabled = TRUE;

-- ── Seed Data (dev defaults) ─────────────────────────────────
-- These are also created by DefaultClientInitializer on startup,
-- but having them in SQL enables database-only deployments.

-- NOTE: client_secret values below are BCrypt hashes.
-- They are inserted by the application (DefaultClientInitializer) at startup,
-- not here, because BCrypt hashes are non-deterministic.
-- This migration only creates the schema.
