-- Owned only by the vm-manager. The backend reads sandbox state through gRPC, never here.

CREATE TABLE sandboxes (
    user_id            text PRIMARY KEY,
    container_id       text        NOT NULL DEFAULT '',
    image_version      text        NOT NULL DEFAULT '',
    state              text        NOT NULL CHECK (state IN ('stopped', 'waking', 'awake', 'busy', 'broken')),
    -- Awake-cap priority from the proto (1 low/free, 2 normal/pro, 3 high/max). Lower is evicted first.
    priority           smallint    NOT NULL DEFAULT 1,
    last_active_at     timestamptz NOT NULL,
    -- Only hashes (lowercase hex sha256). Plaintext lives only in the user's /data volume.
    mcp_token_hash     text        NOT NULL,
    bridge_secret_hash text        NOT NULL,
    -- Extra egress domains for this user's container (Squid dstdomain syntax).
    allowlist          text[]      NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL
);

CREATE UNIQUE INDEX sandboxes_mcp_token_hash_key ON sandboxes (mcp_token_hash);
CREATE INDEX sandboxes_state_idx ON sandboxes (state);

-- The HLD's awake_log: one row per awake period (container running). GetUsage sums these for credits.
-- No foreign key on purpose: rows outlive DeleteSandbox so the last period can still be charged.
CREATE TABLE awake_periods (
    id              bigserial   PRIMARY KEY,
    user_id         text        NOT NULL,
    started_at      timestamptz NOT NULL,
    ended_at        timestamptz,
    browser_seconds bigint      NOT NULL DEFAULT 0 CHECK (browser_seconds >= 0),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

-- At most one open period per user.
CREATE UNIQUE INDEX awake_periods_one_open ON awake_periods (user_id) WHERE ended_at IS NULL;
CREATE INDEX awake_periods_user_started ON awake_periods (user_id, started_at);
CREATE INDEX awake_periods_started ON awake_periods (started_at);
