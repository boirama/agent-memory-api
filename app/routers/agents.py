"""/v1/agents - register and list agents."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import metering
from ..auth import Principal, resolve_principal
from ..db import get_session
from ..schemas import AgentRecord, RegisterAgentRequest, RegisterAgentResponse

router = APIRouter(prefix="/v1/agents", tags=["Agents"])


@router.post(
    "",
    response_model=RegisterAgentResponse,
    summary="Register an agent so it can own and share memory",
    description=(
        "Idempotent - registering an existing agent updates its profile and "
        "refreshes last_seen_at, so you can safely call this on every agent "
        "boot. Registration is what lets an agent appear as a grantee in "
        "POST /v1/permissions."
    ),
)
async def register_agent(
    payload: RegisterAgentRequest,
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> RegisterAgentResponse:
    existed = (
        await session.execute(
            text(
                "SELECT 1 FROM agents WHERE tenant_id = CAST(:tid AS uuid) "
                "AND agent_id = :aid"
            ),
            {"tid": principal.tenant_id, "aid": payload.agent_id},
        )
    ).first() is not None

    row = (
        await session.execute(
            text(
                "INSERT INTO agents (tenant_id, agent_id, display_name, description, "
                "  framework, capabilities, discoverable, metadata) "
                "VALUES (CAST(:tid AS uuid), :aid, :name, :desc, :fw, "
                "        CAST(:caps AS jsonb), :disc, CAST(:meta AS jsonb)) "
                "ON CONFLICT (tenant_id, agent_id) DO UPDATE SET "
                "  display_name = COALESCE(EXCLUDED.display_name, agents.display_name), "
                "  description  = COALESCE(EXCLUDED.description, agents.description), "
                "  framework    = COALESCE(EXCLUDED.framework, agents.framework), "
                "  capabilities = EXCLUDED.capabilities, "
                "  discoverable = EXCLUDED.discoverable, "
                "  metadata     = EXCLUDED.metadata, "
                "  last_seen_at = now() "
                "RETURNING agent_id, display_name, description, framework, "
                "          capabilities, discoverable, created_at, last_seen_at"
            ),
            {
                "tid": principal.tenant_id, "aid": payload.agent_id,
                "name": payload.display_name, "desc": payload.description,
                "fw": payload.framework,
                "caps": json.dumps(payload.capabilities),
                "disc": payload.discoverable,
                "meta": json.dumps(payload.metadata),
            },
        )
    ).first()

    await metering.record(session, principal, "register", agent_id=payload.agent_id)

    return RegisterAgentResponse(
        agent=AgentRecord(
            agent_id=row[0], display_name=row[1], description=row[2],
            framework=row[3], capabilities=row[4] or [], discoverable=row[5],
            created_at=row[6], last_seen_at=row[7],
        ),
        status="updated" if existed else "registered",
    )


@router.get(
    "",
    response_model=list[AgentRecord],
    summary="List the agents registered to your account",
    description="Every agent under your account, most recently active first.",
)
async def list_agents(
    discoverable_only: bool = Query(False),
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> list[AgentRecord]:
    clause = " AND discoverable = true" if discoverable_only else ""
    rows = (
        await session.execute(
            text(
                "SELECT agent_id, display_name, description, framework, "
                "       capabilities, discoverable, created_at, last_seen_at "
                f"FROM agents WHERE tenant_id = CAST(:tid AS uuid){clause} "
                "ORDER BY last_seen_at DESC LIMIT 200"
            ),
            {"tid": principal.tenant_id},
        )
    ).all()
    return [
        AgentRecord(
            agent_id=r[0], display_name=r[1], description=r[2], framework=r[3],
            capabilities=r[4] or [], discoverable=r[5],
            created_at=r[6], last_seen_at=r[7],
        )
        for r in rows
    ]
