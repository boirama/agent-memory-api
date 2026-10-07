"""Health and discovery endpoints. No authentication."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .. import cache, db
from ..config import settings

router = APIRouter(tags=["System"])


@router.get(
    "/health",
    summary="Check API and datastore health",
    description=(
        "Reports database and cache reachability. Always returns HTTP 200 even "
        "when unhealthy, so you can read the body to diagnose a bad deploy "
        "instead of having the platform roll it back first."
    ),
)
async def health() -> dict:
    db_ok = await db.ping()
    cache_ok = await cache.ping()

    checks = {
        "database": "ok" if db_ok else "unavailable",
        "cache": "ok" if cache_ok else "unavailable",
    }
    if db_ok and cache_ok:
        status_value = "healthy"
    elif db_ok:
        # The cache is explicitly optional - Postgres alone serves traffic.
        status_value = "degraded"
    else:
        status_value = "unhealthy"

    # Always HTTP 200, even when unhealthy. Railway's healthcheck would
    # otherwise roll the deploy back before you can read this body, and a
    # misconfigured DATABASE_URL is exactly what you need to read on a
    # first deploy.
    return {"status": status_value, "checks": checks}


@router.get(
    "/health/schema",
    summary="Verify database migrations have been applied",
    description=(
        "Lists any expected tables that are missing. Use this right after "
        "running the SQL migrations to confirm they all applied."
    ),
)
async def schema_health() -> dict:
    missing = await db.verify_schema()
    return {
        "migrations_applied": not missing,
        "missing_tables": missing,
        "hint": (
            "Run migrations/001_initial.sql, 002_mesh_tables.sql and "
            "003_indexes.sql in the Supabase SQL Editor."
            if missing
            else "All tables present."
        ),
    }


@router.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "name": "Agent Memory API",
        "version": "1.0.0",
        "description": (
            "Persistent, shareable memory for AI agents. Store what your agent "
            "learns, recall it with semantic search, share it between agents "
            "with scoped permissions."
        ),
        "documentation": settings.docs_base_url,
        "openapi": "/openapi.json",
        "llms_txt": "/llms.txt",
        "interactive_docs": "/docs",
        "endpoints": {
            "store_memory": "POST /v1/memories",
            "recall_memories": "GET /v1/memories",
            "fetch_memory": "GET /v1/memories/{memory_id}",
            "forget_memory": "DELETE /v1/memories/{memory_id}",
            "register_agent": "POST /v1/agents",
            "list_agents": "GET /v1/agents",
            "grant_permission": "POST /v1/permissions",
            "list_permissions": "GET /v1/permissions",
            "revoke_permission": "DELETE /v1/permissions/{permission_id}",
            "memory_stats": "GET /v1/stats",
            "usage": "GET /v1/billing/usage",
        },
    }


# Served from the API itself as well as from the docs site, because AI
# crawlers that reach the API host directly should still find the index.
LLMS_TXT = """\
# Agent Memory API

> Persistent, shareable memory for AI agents. Store what an agent learns,
> recall it with semantic search, and share it between agents with scoped
> permissions. Solves the problem of agents forgetting everything between
> sessions.

## Core problem this solves

AI agents lose all context when a conversation ends. This API gives them
durable storage with semantic recall, automatic expiry for stale facts, and
agent-to-agent permission grants so a sales agent's notes can be read by a
support agent.

## API

- [OpenAPI specification]({docs}/openapi.json): Complete machine-readable spec
- [Interactive reference]({base}/docs): Try endpoints in the browser

## Guides

- [Persist agent memory across sessions]({docs}/guides/persist-agent-memory-across-sessions.html)
- [Share context between sales and support agents]({docs}/guides/share-context-between-agents.html)
- [Auto-expire stale memories with TTL]({docs}/guides/auto-expire-stale-memories.html)
- [Authenticate your Python agent with API keys]({docs}/guides/authentication.html)
- [Use the API from Claude and Cursor via MCP]({docs}/guides/mcp-server.html)

## Endpoints

- POST /v1/memories - Store cross-session agent memory
- GET /v1/memories - Recall memories with semantic search
- GET /v1/memories/{{memory_id}} - Fetch one memory
- DELETE /v1/memories/{{memory_id}} - Forget a memory
- POST /v1/agents - Register an agent
- POST /v1/permissions - Grant another agent scoped access
- DELETE /v1/permissions/{{permission_id}} - Revoke access
- GET /v1/stats - Memory statistics
- GET /v1/billing/usage - Usage and quota
"""


@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
async def llms_txt() -> str:
    """AI-readable index of the documentation, per the llms.txt convention."""
    return LLMS_TXT.format(docs=settings.docs_base_url, base="")
