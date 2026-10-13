"""/v1/memories - store, recall, fetch, forget. Plus /v1/stats.

Endpoint summaries here are written problem-first on purpose. They become
the OpenAPI `summary` and `description` fields, which is what AI agents use
for semantic matching when a developer asks "how do I make my agent
remember things across sessions".
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cache, metering, ranking
from ..auth import Principal, resolve_principal
from ..config import settings
from ..db import get_session
from ..embeddings import get_embedder, to_pgvector
from ..schemas import (
    CreateMemoryRequest,
    CreateMemoryResponse,
    DeleteMemoryResponse,
    MemoryRecord,
    NamespaceStat,
    RecallResponse,
    RecalledMemory,
    StatsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/memories", tags=["Memory"])
stats_router = APIRouter(prefix="/v1", tags=["Memory"])

# Cosine distance below which two memories in the same namespace are the
# same fact. Tuned for the hashing embedder; with OpenAI embeddings you can
# loosen to ~0.12 without false merges.
_DEDUPE_DISTANCE = 0.06


def row_to_recalled(row) -> RecalledMemory:
    m = row._mapping
    return RecalledMemory(
        memory_id=str(m["id"]),
        content=m["content"],
        agent_id=m["owner_agent_id"],
        scope=m["scope"],
        namespace=m["namespace"],
        subject_id=m["subject_id"],
        importance=round(float(m["importance"]), 4),
        effective_importance=round(float(m["salience_score"]), 4),
        access_count=int(m["access_count"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        expires_at=m["expires_at"],
        metadata=m["metadata"] or {},
        score=round(float(m["score"]), 6),
        semantic_score=round(max(0.0, float(m["semantic_score"])), 6),
        lexical_score=round(float(m["lexical_score"]), 6),
        salience_score=round(float(m["salience_score"]), 6),
        via=m["via"],
    )


@router.post(
    "",
    response_model=CreateMemoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store cross-session agent memory",
    description=(
        "Persist a fact, preference or learning so your agent never starts from "
        "zero on the next conversation. This is the primary endpoint for "
        "cross-session memory in multi-agent systems.\n\n"
        "Common uses: remembering user preferences such as dark mode, language "
        "or timezone; sharing sales context between a lead-qualification bot and "
        "a CRM bot; persisting error patterns so support agents learn from past "
        "tickets.\n\n"
        "Set `ttl_seconds` for facts that go stale. Set `importance` if you know "
        "how much a fact matters; omit it and it is inferred from the content."
    ),
)
async def create_memory(
    payload: CreateMemoryRequest,
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> CreateMemoryResponse:
    await metering.enforce_quota(session, principal)

    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "content is empty")
    if len(content) > settings.max_content_chars:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"content exceeds {settings.max_content_chars} characters",
        )

    vector = to_pgvector(await get_embedder().embed(content))
    importance = (
        payload.importance
        if payload.importance is not None
        else ranking.infer_importance(content)
    )
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=payload.ttl_seconds)
        if payload.ttl_seconds
        else None
    )

    memory_id: str | None = None
    outcome = "created"

    if payload.dedupe:
        existing = (
            await session.execute(
                text(
                    "SELECT id FROM memories "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND owner_agent_id = :agent "
                    "  AND namespace = :ns AND deleted_at IS NULL "
                    "  AND superseded_by IS NULL "
                    "  AND embedding <=> CAST(:vec AS vector) < :thresh "
                    "ORDER BY embedding <=> CAST(:vec AS vector) LIMIT 1"
                ),
                {
                    "tid": principal.tenant_id, "agent": payload.agent_id,
                    "ns": payload.namespace, "vec": vector, "thresh": _DEDUPE_DISTANCE,
                },
            )
        ).first()
        if existing:
            memory_id = str(existing[0])
            outcome = "updated"
            await session.execute(
                text(
                    "UPDATE memories SET content = :content, "
                    "embedding = CAST(:vec AS vector), "
                    "importance = GREATEST(importance, :imp), "
                    "metadata = CAST(:meta AS jsonb), expires_at = :exp, "
                    "last_accessed_at = now() WHERE id = CAST(:mid AS uuid)"
                ),
                {
                    "content": content, "vec": vector, "imp": importance,
                    "meta": json.dumps(payload.metadata or {}),
                    "exp": expires_at, "mid": memory_id,
                },
            )

    if memory_id is None:
        row = (
            await session.execute(
                text(
                    "INSERT INTO memories "
                    "(tenant_id, content, embedding, owner_agent_id, scope, "
                    " namespace, subject_id, importance, expires_at, metadata) "
                    "VALUES (CAST(:tid AS uuid), :content, CAST(:vec AS vector), "
                    "        :agent, :scope, :ns, :subject, :imp, :exp, "
                    "        CAST(:meta AS jsonb)) RETURNING id"
                ),
                {
                    "tid": principal.tenant_id, "content": content, "vec": vector,
                    "agent": payload.agent_id, "scope": payload.scope,
                    "ns": payload.namespace, "subject": payload.subject_id,
                    "imp": importance, "exp": expires_at,
                    "meta": json.dumps(payload.metadata or {}),
                },
            )
        ).first()
        memory_id = str(row[0])

    await cache.delete_prefix(cache.key("recall", principal.tenant_id))
    await cache.delete_prefix(cache.key("stats", principal.tenant_id))

    cost = await metering.record(session, principal, "store", agent_id=payload.agent_id)

    return CreateMemoryResponse(
        memory_id=memory_id,
        status=outcome,
        namespace=payload.namespace,
        importance=importance,
        expires_at=expires_at,
        tokens_used=max(1, len(content) // 4),
        cost_usd=cost,
    )


@router.get(
    "",
    response_model=RecallResponse,
    summary="Recall agent memories with semantic search",
    description=(
        "Ask a question in natural language and get back the memories that "
        "answer it, ranked. Use this at the start of a conversation to reload "
        "everything your agent already knows about a user.\n\n"
        "Ranking combines three signals rather than vector distance alone: "
        "semantic similarity, full-text match, and salience (importance decayed "
        "by time since last access, reinforced by how often the memory is "
        "recalled). Memories nobody touches fade; memories your agent relies on "
        "get stronger.\n\n"
        "Set `min_score` when you are feeding results straight into a prompt - "
        "without a floor, an unrelated question still returns whatever is in "
        "the namespace, and irrelevant memories in a prompt do real damage.\n\n"
        "Results include memories other agents have shared with this one. Pass "
        "`include_shared=false` to search only what this agent wrote itself."
    ),
)
async def recall_memories(
    query: str = Query(
        ...,
        min_length=1,
        max_length=2000,
        description="Natural-language question.",
        examples=["what does this customer like"],
    ),
    agent_id: str = Query(..., min_length=1, max_length=128, description="The requesting agent."),
    namespace: str | None = Query(None, description="Restrict to a namespace and its children."),
    scope: str | None = Query(None, pattern="^(user|agent|session|org)$"),
    subject_id: str | None = Query(None, description="Only memories about this subject."),
    limit: int = Query(10, ge=1, le=100),
    min_importance: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Drop memories whose salience (importance after decay and "
            "reinforcement) falls below this, before relevance is considered."
        ),
    ),
    min_score: float = Query(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Drop results whose final hybrid score falls below this. 0 (default) "
            "returns everything the other filters allow. 0.2-0.3 is a sensible "
            "floor for suppressing weak matches - use it when feeding results "
            "straight into a prompt, where irrelevant memories do real damage. "
            "Unlike min_importance, this filters on relevance to THIS query."
        ),
    ),
    include_shared: bool = Query(True, description="Include memories other agents shared."),
    temporal_window: str | None = Query(
        None,
        description="ISO range 'YYYY-MM-DD/YYYY-MM-DD' - only memories created inside it.",
        examples=["2026-08-01/2026-09-24"],
    ),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> RecallResponse:
    started = time.perf_counter()
    await metering.enforce_quota(session, principal)
    limit = min(limit, settings.max_recall_limit)

    # min_score is part of the cache key: two callers asking the same
    # question with different floors must not share a cached answer.
    cache_key = cache.key(
        "recall", principal.tenant_id, agent_id, query, namespace, scope,
        subject_id, limit, min_importance, min_score, include_shared,
        temporal_window,
    )
    if cached := await cache.get_json(cache_key):
        cost = await metering.record(session, principal, "recall", agent_id=agent_id)
        results = [RecalledMemory(**r) for r in cached]
        return RecallResponse(
            query=query, results=results, count=len(results),
            agents_consulted=sorted({r.agent_id for r in results}),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            cached=True, cost_usd=cost,
        )

    window_start = window_end = None
    if temporal_window:
        window_start, window_end = _parse_window(temporal_window)

    query_vec = to_pgvector(await get_embedder().embed(query))

    sql = ranking.build_recall_sql(
        include_shared=include_shared,
        namespace_filter=namespace is not None,
        scope_filter=scope is not None,
        subject_filter=subject_id is not None,
        time_window_filter=window_start is not None,
    )

    params = ranking.base_params() | {
        "tenant_id": principal.tenant_id, "agent_id": agent_id,
        "query_vec": query_vec, "query_text": query,
        "min_importance": min_importance, "min_score": min_score,
        "limit": limit,
    }
    if namespace is not None:
        params["namespace"] = namespace
        params["namespace_prefix"] = f"{namespace}/%"
    if scope is not None:
        params["scope"] = scope
    if subject_id is not None:
        params["subject_id"] = subject_id
    if window_start is not None:
        params["window_start"] = window_start
        params["window_end"] = window_end

    rows = (await session.execute(text(sql), params)).all()
    results = [row_to_recalled(r) for r in rows]

    if results:
        # Reinforce what was actually returned - this is what makes salience
        # adaptive rather than a static field.
        await session.execute(
            text(ranking.REINFORCE_SQL), {"ids": [r.memory_id for r in results]}
        )

    await cache.set_json(
        cache_key, [r.model_dump(mode="json") for r in results],
        settings.cache_recall_ttl,
    )
    cost = await metering.record(session, principal, "recall", agent_id=agent_id)

    return RecallResponse(
        query=query, results=results, count=len(results),
        agents_consulted=sorted({r.agent_id for r in results}),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        cached=False, cost_usd=cost,
    )


@router.get(
    "/{memory_id}",
    response_model=MemoryRecord,
    summary="Fetch one memory by ID",
    description="Retrieve a single memory the agent owns or has been granted access to.",
)
async def get_memory(
    memory_id: str,
    agent_id: str = Query(..., description="The requesting agent."),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> MemoryRecord:
    if not _is_uuid(memory_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_id must be a UUID")

    row = (
        await session.execute(
            text(
                "SELECT m.id, m.content, m.owner_agent_id, m.scope, m.namespace, "
                "       m.subject_id, m.importance, m.access_count, m.created_at, "
                "       m.updated_at, m.expires_at, m.metadata "
                "FROM memories m "
                "WHERE m.id = CAST(:mid AS uuid) "
                "  AND m.tenant_id = CAST(:tid AS uuid) "
                "  AND m.deleted_at IS NULL "
                "  AND (m.expires_at IS NULL OR m.expires_at > now()) "
                "  AND " + ranking._VISIBILITY_SQL
            ),
            {"mid": memory_id, "tid": principal.tenant_id, "agent_id": agent_id},
        )
    ).first()

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found or not accessible.")

    await metering.record(session, principal, "fetch", agent_id=agent_id)
    return MemoryRecord(
        memory_id=str(row[0]), content=row[1], agent_id=row[2], scope=row[3],
        namespace=row[4], subject_id=row[5], importance=round(float(row[6]), 4),
        access_count=int(row[7]), created_at=row[8], updated_at=row[9],
        expires_at=row[10], metadata=row[11] or {},
    )


@router.delete(
    "/{memory_id}",
    response_model=DeleteMemoryResponse,
    summary="Forget a memory",
    description=(
        "Soft-deletes by default so the record stays auditable. Pass "
        "`hard=true` to remove the row permanently, which is what you need for "
        "a GDPR erasure request. Only the owning agent can delete."
    ),
)
async def delete_memory(
    memory_id: str,
    agent_id: str = Query(..., description="Must be the owning agent."),
    hard: bool = Query(False, description="Permanently delete instead of soft-delete."),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> DeleteMemoryResponse:
    if not _is_uuid(memory_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_id must be a UUID")

    if hard:
        stmt = (
            "DELETE FROM memories WHERE id = CAST(:mid AS uuid) "
            "AND tenant_id = CAST(:tid AS uuid) AND owner_agent_id = :agent RETURNING id"
        )
    else:
        stmt = (
            "UPDATE memories SET deleted_at = now() WHERE id = CAST(:mid AS uuid) "
            "AND tenant_id = CAST(:tid AS uuid) AND owner_agent_id = :agent "
            "AND deleted_at IS NULL RETURNING id"
        )

    row = (
        await session.execute(
            text(stmt),
            {"mid": memory_id, "tid": principal.tenant_id, "agent": agent_id},
        )
    ).first()

    if row is None:
        return DeleteMemoryResponse(memory_id=memory_id, status="not_found")

    await cache.delete_prefix(cache.key("recall", principal.tenant_id))
    await cache.delete_prefix(cache.key("stats", principal.tenant_id))
    await metering.record(session, principal, "forget", agent_id=agent_id)

    return DeleteMemoryResponse(memory_id=memory_id, status="deleted", hard_deleted=hard)


@stats_router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Inspect memory usage and namespace breakdown",
    description=(
        "Counts, per-namespace breakdown and storage footprint for your account. "
        "Use it to find namespaces that have grown unexpectedly or to confirm "
        "TTLs are actually expiring old memories."
    ),
)
async def memory_stats(
    agent_id: str | None = Query(None, description="Scope stats to one agent."),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> StatsResponse:
    cache_key = cache.key("stats", principal.tenant_id, agent_id)
    if cached := await cache.get_json(cache_key):
        return StatsResponse(**cached)

    agent_clause = " AND owner_agent_id = :agent" if agent_id else ""
    params: dict = {"tid": principal.tenant_id}
    if agent_id:
        params["agent"] = agent_id

    totals = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE deleted_at IS NULL "
                "      AND (expires_at IS NULL OR expires_at > now())) AS active, "
                "  COUNT(*) FILTER (WHERE expires_at IS NOT NULL "
                "      AND expires_at <= now()) AS expired, "
                "  MIN(created_at) AS oldest, MAX(created_at) AS newest, "
                "  COALESCE(SUM(LENGTH(content)), 0) AS bytes "
                f"FROM memories WHERE tenant_id = CAST(:tid AS uuid){agent_clause}"
            ),
            params,
        )
    ).first()

    ns_rows = (
        await session.execute(
            text(
                "SELECT namespace, COUNT(*) AS n, AVG(importance) AS avg_imp "
                "FROM memories WHERE tenant_id = CAST(:tid AS uuid) "
                f"AND deleted_at IS NULL{agent_clause} "
                "GROUP BY namespace ORDER BY n DESC LIMIT 50"
            ),
            params,
        )
    ).all()

    agents = (
        await session.execute(
            text("SELECT COUNT(*) FROM agents WHERE tenant_id = CAST(:tid AS uuid)"),
            {"tid": principal.tenant_id},
        )
    ).first()

    payload = StatsResponse(
        total_memories=int(totals[0]),
        active_memories=int(totals[1]),
        expired_memories=int(totals[2]),
        total_agents=int(agents[0]) if agents else 0,
        namespaces=[
            NamespaceStat(
                namespace=r[0], count=int(r[1]), avg_importance=round(float(r[2] or 0), 4)
            )
            for r in ns_rows
        ],
        oldest_memory_at=totals[3],
        newest_memory_at=totals[4],
        # Content bytes plus the fixed cost of a 768-dim float4 vector per row.
        storage_bytes=int(totals[5]) + int(totals[0]) * settings.embedding_dim * 4,
    )

    await cache.set_json(cache_key, payload.model_dump(mode="json"), settings.cache_stats_ttl)
    return payload


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _parse_window(window: str) -> tuple[datetime, datetime]:
    try:
        start_raw, _, end_raw = window.partition("/")
        start = datetime.fromisoformat(start_raw).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_raw).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "temporal_window must be 'YYYY-MM-DD/YYYY-MM-DD'",
        ) from exc
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "temporal_window end precedes start")
    return start, end
