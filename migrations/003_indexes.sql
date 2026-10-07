-- ============================================================
-- Agent Memory API — Migration 003: indexes
-- Run this THIRD, after 002_mesh_tables.sql.
--
-- Kept separate from table definitions so you can drop and rebuild
-- indexes later without touching data.
-- ============================================================

-- Vector index — the semantic half of hybrid search.
-- HNSW needs no training step, so it works on an empty table.
-- vector_cosine_ops matches the <=> operator in app/ranking.py;
-- change one and you must change the other.
CREATE INDEX IF NOT EXISTS memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- On pgvector < 0.5.0 HNSW is unavailable. Use IVFFlat instead, but
-- build it AFTER you have rows or recall is poor:
--   CREATE INDEX memories_embedding_ivf ON memories
--       USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Full-text index — the lexical half of hybrid search.
CREATE INDEX IF NOT EXISTS memories_search_vector_gin
    ON memories USING gin (search_vector);

-- No trigram index here on purpose: websearch_to_tsquery already stems,
-- an unqueried GIN index taxes every INSERT, and pg_trgm is missing from
-- several managed Postgres builds.

-- Filter indexes. The partial WHERE matters — every recall filters
-- deleted_at IS NULL, so excluding dead rows keeps these small and hot.
CREATE INDEX IF NOT EXISTS memories_tenant_live
    ON memories (tenant_id, namespace, owner_agent_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS memories_tenant_scope
    ON memories (tenant_id, scope, subject_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS memories_expiry_sweep
    ON memories (expires_at)
    WHERE deleted_at IS NULL AND expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS memories_recency
    ON memories (tenant_id, created_at DESC)
    WHERE deleted_at IS NULL;

-- Permission lookups — hit on every cross-agent recall.
CREATE INDEX IF NOT EXISTS mesh_grants_lookup
    ON mesh_grants (tenant_id, grantee_agent_id, namespace)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS mesh_shares_lookup
    ON mesh_shares (tenant_id, to_agent_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS agents_discoverable
    ON agents (tenant_id, discoverable)
    WHERE discoverable = true;

CREATE INDEX IF NOT EXISTS usage_events_rollup
    ON usage_events (tenant_id, billing_period, operation);

CREATE INDEX IF NOT EXISTS api_keys_lookup
    ON api_keys (key_hash)
    WHERE revoked_at IS NULL;
