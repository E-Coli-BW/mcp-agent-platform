-- Target schema for the production Postgres runtime adapter.
-- SQLiteRuntimeStore mirrors these ownership boundaries locally.
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_run_events (
  run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
  event_seq BIGINT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, event_seq),
  UNIQUE (event_id)
);

CREATE TABLE IF NOT EXISTS agent_tool_invocations (
  idempotency_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TIMESTAMPTZ,
  result_json JSONB,
  error_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_status
  ON agent_runs(tenant_id, status, updated_at DESC);
