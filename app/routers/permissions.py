"""/v1/permissions - agent-to-agent access grants.

This is the differentiator a plain vector store can't offer: one agent
grants another scoped access to its memory. It's also the thing worth
charging more for - a grant is priced 10x a recall because it's a
high-value, low-frequency action.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta, timezone

from .. import cache, metering
from ..auth import Principal, resolve_principal
from ..db import get_session
from ..schemas import (
    GrantPermissionRequest,
    GrantPermissionResponse,
    PermissionRecord,
    RevokePermissionResponse,
)

router = APIRouter(prefix="/v1/permissions", tags=["Permissions"])


@router.post(
    "",
    response_model=GrantPermissionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Share context between agents with scoped permissions",
    description=(
        "Let one agent read another's memory, like sharing a folder in Google "
        "Drive. Two modes:\n\n"
        "**Namespace grant** - pass `namespace` for standing access to "
        "everything in it and its children. A grant on 'acme/sales' also covers "
        "'acme/sales/leads'. This is what you want for a sales agent handing "
        "context to a support agent.\n\n"
        "**Single memory** - pass `memory_id` to hand over one specific fact.\n\n"
        "Set `grantee_agent_id` to '*' to share with every agent in your "
        "account. Set `ttl_seconds` to auto-revoke. Grants are revocable at any "
        "time via DELETE /v1/permissions/{permission_id}."
    ),
)
async def grant_permission(
    payload: GrantPermissionRequest,
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> GrantPermissionResponse:
    await metering.enforce_quota(session, principal)

    if bool(payload.memory_id) == bool(payload.namespace):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide exactly one of namespace or memory_id.",
        )
    if payload.granter_agent_id == payload.grantee_agent_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "An agent cannot grant access to itself."
        )

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=payload.ttl_seconds)
        if payload.ttl_seconds
        else None
    )

    if payload.memory_id:
        if not _is_uuid(payload.memory_id):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_id must be a UUID")
        if payload.permission == "admin":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "admin applies to namespaces, not individual memories.",
            )

        owned = (
            await session.execute(
                text(
                    "SELECT 1 FROM memories WHERE id = CAST(:mid AS uuid) "
                    "AND tenant_id = CAST(:tid AS uuid) AND owner_agent_id = :agent "
                    "AND deleted_at IS NULL"
                ),
                {
                    "mid": payload.memory_id, "tid": principal.tenant_id,
                    "agent": payload.granter_agent_id,
                },
            )
        ).first()
        if owned is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Memory not found, or not owned by granter_agent_id.",
            )

        row = (
            await session.execute(
                text(
                    "INSERT INTO mesh_shares (tenant_id, memory_id, from_agent_id, "
                    "  to_agent_id, permission, note, expires_at) "
                    "VALUES (CAST(:tid AS uuid), CAST(:mid AS uuid), :from_a, "
                    "        :to_a, :perm, :note, :exp) "
                    "ON CONFLICT (tenant_id, memory_id, to_agent_id) "
                    "  WHERE revoked_at IS NULL "
                    "DO UPDATE SET permission = EXCLUDED.permission, "
                    "              expires_at = EXCLUDED.expires_at, "
                    "              note = EXCLUDED.note "
                    "RETURNING id, created_at"
                ),
                {
                    "tid": principal.tenant_id, "mid": payload.memory_id,
                    "from_a": payload.granter_agent_id,
                    "to_a": payload.grantee_agent_id,
                    "perm": payload.permission, "note": payload.note,
                    "exp": expires_at,
                },
            )
        ).first()
        kind = "memory"
    else:
        row = (
            await session.execute(
                text(
                    "INSERT INTO mesh_grants (tenant_id, granter_agent_id, "
                    "  grantee_agent_id, namespace, permission, expires_at) "
                    "VALUES (CAST(:tid AS uuid), :from_a, :to_a, :ns, :perm, :exp) "
                    "ON CONFLICT (tenant_id, granter_agent_id, grantee_agent_id, "
                    "             namespace) WHERE revoked_at IS NULL "
                    "DO UPDATE SET permission = EXCLUDED.permission, "
                    "              expires_at = EXCLUDED.expires_at "
                    "RETURNING id, created_at"
                ),
                {
                    "tid": principal.tenant_id,
                    "from_a": payload.granter_agent_id,
                    "to_a": payload.grantee_agent_id,
                    "ns": payload.namespace, "perm": payload.permission,
                    "exp": expires_at,
                },
            )
        ).first()
        kind = "namespace"

    await cache.delete_prefix(cache.key("recall", principal.tenant_id))
    cost = await metering.record(
        session, principal, "permission", agent_id=payload.granter_agent_id
    )

    return GrantPermissionResponse(
        permission_id=str(row[0]), kind=kind,
        granter_agent_id=payload.granter_agent_id,
        grantee_agent_id=payload.grantee_agent_id,
        namespace=payload.namespace, memory_id=payload.memory_id,
        permission=payload.permission, expires_at=expires_at,
        created_at=row[1], cost_usd=cost,
    )


@router.get(
    "",
    response_model=list[PermissionRecord],
    summary="List active permission grants",
    description="Every live grant and share in your account. Revoked and expired entries are excluded.",
)
async def list_permissions(
    agent_id: str | None = Query(None, description="Filter to grants involving this agent."),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> list[PermissionRecord]:
    clause = ""
    params: dict = {"tid": principal.tenant_id}
    if agent_id:
        clause = " AND (granter_agent_id = :aid OR grantee_agent_id = :aid)"
        params["aid"] = agent_id

    grants = (
        await session.execute(
            text(
                "SELECT id, granter_agent_id, grantee_agent_id, namespace, "
                "       permission, expires_at, created_at "
                "FROM mesh_grants WHERE tenant_id = CAST(:tid AS uuid) "
                "AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())" + clause +
                " ORDER BY created_at DESC LIMIT 200"
            ),
            params,
        )
    ).all()

    share_clause = ""
    if agent_id:
        share_clause = " AND (from_agent_id = :aid OR to_agent_id = :aid)"
    shares = (
        await session.execute(
            text(
                "SELECT id, from_agent_id, to_agent_id, memory_id, permission, "
                "       expires_at, created_at "
                "FROM mesh_shares WHERE tenant_id = CAST(:tid AS uuid) "
                "AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())" + share_clause +
                " ORDER BY created_at DESC LIMIT 200"
            ),
            params,
        )
    ).all()

    out = [
        PermissionRecord(
            permission_id=str(r[0]), kind="namespace", granter_agent_id=r[1],
            grantee_agent_id=r[2], namespace=r[3], permission=r[4],
            expires_at=r[5], created_at=r[6],
        )
        for r in grants
    ]
    out += [
        PermissionRecord(
            permission_id=str(r[0]), kind="memory", granter_agent_id=r[1],
            grantee_agent_id=r[2], memory_id=str(r[3]), permission=r[4],
            expires_at=r[5], created_at=r[6],
        )
        for r in shares
    ]
    return out


@router.delete(
    "/{permission_id}",
    response_model=RevokePermissionResponse,
    summary="Revoke an agent's access",
    description=(
        "Revokes by ID. Works for both namespace grants and single-memory "
        "shares. Takes effect on the next recall - cached results for the "
        "account are invalidated immediately."
    ),
)
async def revoke_permission(
    permission_id: str,
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> RevokePermissionResponse:
    if not _is_uuid(permission_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "permission_id must be a UUID")

    revoked = False
    for table in ("mesh_grants", "mesh_shares"):
        row = (
            await session.execute(
                text(
                    f"UPDATE {table} SET revoked_at = now() "
                    "WHERE id = CAST(:pid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "AND revoked_at IS NULL RETURNING id"
                ),
                {"pid": permission_id, "tid": principal.tenant_id},
            )
        ).first()
        if row is not None:
            revoked = True
            break

    await cache.delete_prefix(cache.key("recall", principal.tenant_id))
    return RevokePermissionResponse(
        permission_id=permission_id, status="revoked" if revoked else "not_found"
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False
