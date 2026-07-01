-- V2: PostgreSQL Row-Level Security for tenant isolation
-- Defense-in-depth: even raw SQL queries are tenant-scoped

-- Enable RLS on memories table
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

-- Force RLS even for table owner (important for superuser protection)
ALTER TABLE memories FORCE ROW LEVEL SECURITY;

-- Policy: each session can only see rows matching current_setting('app.tenant_id')
-- The application must SET app.tenant_id = '<tenantId>' on each connection
CREATE POLICY tenant_isolation ON memories
    USING (tenant_id = current_setting('app.tenant_id', TRUE))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', TRUE));

COMMENT ON POLICY tenant_isolation ON memories IS
    'Row-Level Security: restricts all operations to rows matching the session tenant_id. '
    'Application must call SET app.tenant_id before any query.';
