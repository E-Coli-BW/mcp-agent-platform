-- V3: Add PostgreSQL full-text search support
-- Uses tsvector + GIN index for efficient keyword search
-- instead of LIKE '%keyword%' which does a sequential scan.

-- Add a generated tsvector column combining key + content
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(key, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(content, '')), 'B')
    ) STORED;

-- GIN index for fast full-text search
CREATE INDEX IF NOT EXISTS idx_memories_search_vector
    ON memories USING GIN (search_vector);

-- Composite index for tenant + full-text (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_memories_tenant_search
    ON memories (tenant_id) INCLUDE (key, namespace);
