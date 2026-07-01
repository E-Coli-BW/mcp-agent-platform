-- V1: Initial schema for Memory MCP Server
-- Multi-tenant shared-table design with defense-in-depth

CREATE TABLE IF NOT EXISTS memories (
    id              VARCHAR(36)     PRIMARY KEY,
    tenant_id       VARCHAR(128)    NOT NULL,
    "key"           VARCHAR(512)    NOT NULL,
    content         TEXT            NOT NULL,
    namespace       VARCHAR(128)    NOT NULL DEFAULT 'default',
    tags            TEXT,           -- JSON array, e.g. ["tag1","tag2"]
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,
    access_count    INTEGER         NOT NULL DEFAULT 0,
    pinned          BOOLEAN         NOT NULL DEFAULT FALSE,
    version         BIGINT          DEFAULT 0,

    -- Unique constraint: one key per tenant
    CONSTRAINT uk_tenant_key UNIQUE (tenant_id, "key")
);

-- Index: fast lookup by tenant + namespace
CREATE INDEX IF NOT EXISTS idx_tenant_ns ON memories (tenant_id, namespace);

-- Index: recent entries per tenant (for context/list)
CREATE INDEX IF NOT EXISTS idx_tenant_updated ON memories (tenant_id, updated_at DESC);

-- Index: tenant-only queries (count, list all)
CREATE INDEX IF NOT EXISTS idx_tenant_id ON memories (tenant_id);

COMMENT ON TABLE memories IS 'Multi-tenant memory store for MCP server. All queries must filter by tenant_id.';
COMMENT ON COLUMN memories.tenant_id IS 'Tenant identifier from JWT. All access is scoped to this.';
COMMENT ON COLUMN memories."key" IS 'Unique key within a tenant. Used as the primary lookup identifier.';
COMMENT ON COLUMN memories.tags IS 'JSON array of string tags for categorization and search boosting.';
COMMENT ON COLUMN memories.version IS 'Optimistic lock version. Incremented on every update.';
