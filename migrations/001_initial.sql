-- ============================================================
-- Agent Memory API — Migration 001: core tables
-- Run this FIRST in the Supabase SQL Editor.
-- Safe to re-run (everything is IF NOT EXISTS).
-- ============================================================

-- pgvector is the only hard dependency. gen_random_uuid() has been in
-- Postgres core since v13, so pgcrypto is NOT required — requiring it
-- breaks on several managed Postgres builds for no benefit.
CREATE EXTENSION IF NOT EXISTS "vector";

-- ------------------------------------------------------------
-- tenants
-- One row per paying customer. Traffic via RapidAPI keys off the
-- X-RapidAPI-User header; direct traffic keys off the API key.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id     TEXT NOT NULL UNIQUE,
    source          TEXT NOT NULL DEFAULT 'rapidapi',
    plan            TEXT NOT NULL DEFAULT 'BASIC',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ------------------------------------------------------------
-- api_keys — direct (non-marketplace) customers only
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    label           TEXT,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ
);

-- ------------------------------------------------------------
-- memories
-- Embedding dimension 768 matches both the built-in hashing embedder
-- and OpenAI text-embedding-3-small requested at dimensions=768.
-- Changing it means changing EMBEDDING_DIM and rebuilding the index.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    content             TEXT NOT NULL,
    embedding           vector(768),

    owner_agent_id      TEXT NOT NULL,
    scope               TEXT NOT NULL DEFAULT 'agent',
    namespace           TEXT NOT NULL DEFAULT 'default',
    subject_id          TEXT,

    importance          REAL NOT NULL DEFAULT 0.5,
    access_count        INTEGER NOT NULL DEFAULT 0,
    last_accessed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    expires_at          TIMESTAMPTZ,
    superseded_by       UUID REFERENCES memories(id) ON DELETE SET NULL,

    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,

    search_vector       tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    CONSTRAINT memories_importance_range CHECK (importance >= 0.0 AND importance <= 1.0),
    CONSTRAINT memories_scope_valid CHECK (scope IN ('user', 'agent', 'session', 'org'))
);

-- ------------------------------------------------------------
-- usage_events — append-only meter
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    operation       TEXT NOT NULL,
    units           INTEGER NOT NULL DEFAULT 1,
    unit_cost_usd   NUMERIC(12, 8) NOT NULL DEFAULT 0,
    agent_id        TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    billing_period  TEXT NOT NULL
);

CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_touch_updated_at ON memories;
CREATE TRIGGER memories_touch_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS tenants_touch_updated_at ON tenants;
CREATE TRIGGER tenants_touch_updated_at
    BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
