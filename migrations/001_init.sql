-- Alephat multi-service schema (docs/SERVICE_CONTRACTS.md)
-- Apply with: psql $DATABASE_URL -f migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS orgs (
    org_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS org_memberships (
    org_id UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE IF NOT EXISTS org_settings (
    org_id UUID PRIMARY KEY REFERENCES orgs(org_id) ON DELETE CASCADE,
    default_model TEXT NOT NULL DEFAULT 'openai:gpt-4o-mini',
    default_sandbox_provider TEXT NOT NULL DEFAULT 'agent_sandbox',
    enabled_sandbox_providers TEXT[] NOT NULL DEFAULT ARRAY['daytona', 'agent_sandbox', 'opensandbox'],
    mcp_stdio_allowed BOOLEAN NOT NULL DEFAULT false,
    max_mcp_servers_per_run INT NOT NULL DEFAULT 10,
    sandbox_fallback BOOLEAN NOT NULL DEFAULT false,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id UUID PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    preferred_model TEXT,
    preferred_sandbox_provider TEXT,
    default_mcp_server_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS secrets (
    secret_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID REFERENCES orgs(org_id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, user_id, name)
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    mcp_server_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope TEXT NOT NULL CHECK (scope IN ('user', 'org')),
    org_id UUID REFERENCES orgs(org_id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    transport TEXT NOT NULL CHECK (transport IN ('stdio', 'sse', 'streamable_http')),
    url TEXT,
    command TEXT,
    args JSONB NOT NULL DEFAULT '[]'::jsonb,
    auth_ciphertext TEXT,
    headers_ciphertext TEXT,
    enabled BOOLEAN NOT NULL DEFAULT true,
    tool_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    tool_denylist TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    agent_types TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    execution TEXT NOT NULL DEFAULT 'harness' CHECK (execution IN ('harness', 'sandbox')),
    required BOOLEAN NOT NULL DEFAULT false,
    last_test_at TIMESTAMPTZ,
    last_test_ok BOOLEAN,
    last_tools TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mcp_servers_org ON mcp_servers(org_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_servers_user ON mcp_servers(user_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS tasks (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(org_id),
    thread_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    agent_type TEXT NOT NULL DEFAULT 'coding',
    status TEXT NOT NULL DEFAULT 'queued',
    title TEXT,
    repo TEXT,
    base_ref TEXT,
    prompt TEXT,
    model TEXT NOT NULL,
    sandbox_provider TEXT NOT NULL DEFAULT 'agent_sandbox',
    mcp_server_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    mcp_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    park_reason TEXT,
    cancel_requested_at TIMESTAMPTZ,
    cancel_reason TEXT,
    active_run_id UUID,
    created_by UUID,
    retry_count INT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_org_status ON tasks(org_id, status);

ALTER TABLE org_settings
    ALTER COLUMN default_sandbox_provider SET DEFAULT 'agent_sandbox';
ALTER TABLE tasks
    ALTER COLUMN sandbox_provider SET DEFAULT 'agent_sandbox';

CREATE TABLE IF NOT EXISTS runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INT NOT NULL DEFAULT 1,
    worker_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_lease ON runs(status, lease_expires_at)
    WHERE status IN ('leased', 'active');

CREATE TABLE IF NOT EXISTS messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'user',
    kind TEXT NOT NULL DEFAULT 'user_input',
    content TEXT NOT NULL DEFAULT '',
    blocks JSONB NOT NULL DEFAULT '[]'::jsonb,
    visibility TEXT NOT NULL DEFAULT 'all',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    seq BIGSERIAL
);

CREATE INDEX IF NOT EXISTS idx_messages_task_seq ON messages(task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'plan_approval',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by UUID,
    decided_at TIMESTAMPTZ,
    comment TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id, status);

CREATE TABLE IF NOT EXISTS run_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id UUID,
    seq BIGSERIAL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_run_events_task_seq ON run_events(task_id, seq);

CREATE TABLE IF NOT EXISTS sandboxes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    sandbox_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    provider_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, provider, sandbox_id)
);

CREATE TABLE IF NOT EXISTS graph_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    thread_key TEXT NOT NULL DEFAULT 'default',
    version BIGINT NOT NULL,
    blob JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, thread_key, version)
);

CREATE INDEX IF NOT EXISTS idx_graph_checkpoints_run ON graph_checkpoints(run_id, version DESC);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    provider TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    payload_hash TEXT,
    task_id UUID,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, delivery_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox(created_at) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS processed_messages (
    msg_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS command_idempotency (
    idempotency_key TEXT NOT NULL,
    org_id UUID NOT NULL,
    route TEXT NOT NULL,
    response_status INT NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, route, idempotency_key)
);

-- Seed default org/user for local/dev
INSERT INTO orgs (org_id, name)
VALUES ('00000000-0000-4000-8000-000000000001', 'default')
ON CONFLICT DO NOTHING;

INSERT INTO users (user_id, email, display_name)
VALUES ('00000000-0000-4000-8000-000000000002', 'dev@localhost', 'Dev User')
ON CONFLICT DO NOTHING;

INSERT INTO org_memberships (org_id, user_id, role)
VALUES (
    '00000000-0000-4000-8000-000000000001',
    '00000000-0000-4000-8000-000000000002',
    'admin'
)
ON CONFLICT DO NOTHING;

INSERT INTO org_settings (org_id)
VALUES ('00000000-0000-4000-8000-000000000001')
ON CONFLICT DO NOTHING;

INSERT INTO user_settings (user_id)
VALUES ('00000000-0000-4000-8000-000000000002')
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS allowed_slack_bots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
    team_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    slack_user_id TEXT NOT NULL DEFAULT '',
    app_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    image_url TEXT NOT NULL DEFAULT '',
    created_by UUID REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, team_id, bot_id)
);
