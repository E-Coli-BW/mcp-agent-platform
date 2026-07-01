-- Skill Store schema migration
-- Compatible with both H2 (test) and PostgreSQL (prod)

CREATE TABLE IF NOT EXISTS skills (
    id              VARCHAR(36) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    key             VARCHAR(255) NOT NULL,
    version         INT NOT NULL DEFAULT 1,
    status          VARCHAR(16) NOT NULL DEFAULT 'active',

    title           VARCHAR(255) NOT NULL,
    category        VARCHAR(64),
    problem         TEXT NOT NULL,
    preconditions   TEXT,
    steps           TEXT NOT NULL,
    expected_outcome TEXT,
    pitfalls        TEXT,

    trigger_patterns TEXT,
    trigger_tools   TEXT,
    trigger_errors  TEXT,

    depends_on      TEXT,
    tags            TEXT,

    use_count       INT NOT NULL DEFAULT 0,
    success_count   INT NOT NULL DEFAULT 0,
    failure_count   INT NOT NULL DEFAULT 0,

    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    created_by      VARCHAR(128),

    CONSTRAINT uk_tenant_key_version UNIQUE (tenant_id, key, version)
);

CREATE INDEX IF NOT EXISTS idx_skills_tenant_status ON skills(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_skills_tenant_category ON skills(tenant_id, category);
