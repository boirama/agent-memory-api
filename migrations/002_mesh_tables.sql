-- ============================================================
-- Agent Memory API — Migration 002: agents and permissions
-- Run this SECOND, after 001_initial.sql.
--
-- This is the differentiator: agents register themselves, then
-- grant OTHER agents scoped access to their namespaces. A sales
-- agent's notes become readable by the support agent without
-- either one owning the other's data.
-- ============================================================

CREATE TABLE IF NOT EXISTS agents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    agent_id        TEXT NOT NULL,
    display_name    TEXT,
    description     TEXT,
    framework       TEXT,
    capabilities    JSONB NOT NULL DEFAULT '[]'::jsonb,
    discoverable    BOOLEAN NOT NULL DEFAULT false,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT agents_tenant_agent_unique UNIQUE (tenant_id, agent_id)
);

-- Namespace-level grant. Hierarchical: a grant on 'acme/sales' also
-- covers 'acme/sales/leads'.
CREATE TABLE IF NOT EXISTS mesh_grants (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    granter_agent_id    TEXT NOT NULL,
    grantee_agent_id    TEXT NOT NULL,
    namespace           TEXT NOT NULL,
    permission          TEXT NOT NULL,

    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT mesh_grants_permission_valid CHECK (permission IN ('read', 'write', 'admin')),
    CONSTRAINT mesh_grants_no_self CHECK (granter_agent_id <> grantee_agent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS mesh_grants_live_unique
    ON mesh_grants (tenant_id, granter_agent_id, grantee_agent_id, namespace)
    WHERE revoked_at IS NULL;

-- Direct hand-off of one specific memory to one specific agent.
CREATE TABLE IF NOT EXISTS mesh_shares (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    memory_id           UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,

    from_agent_id       TEXT NOT NULL,
    to_agent_id         TEXT NOT NULL,
    permission          TEXT NOT NULL DEFAULT 'read',
    note                TEXT,

    expires_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT mesh_shares_permission_valid CHECK (permission IN ('read', 'write'))
);

CREATE UNIQUE INDEX IF NOT EXISTS mesh_shares_live_unique
    ON mesh_shares (tenant_id, memory_id, to_agent_id)
    WHERE revoked_at IS NULL;

DROP TRIGGER IF EXISTS agents_touch_updated_at ON agents;
CREATE TRIGGER agents_touch_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
