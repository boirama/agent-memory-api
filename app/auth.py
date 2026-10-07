"""Authentication and multi-tenancy.

Two ways in:

1. Through RapidAPI. Their proxy adds X-RapidAPI-User (who the caller is),
   X-RapidAPI-Subscription (which plan) and X-RapidAPI-Proxy-Secret (proof
   the request really came through them). We trust the first two only
   because the third checks out.

2. Direct, with Authorization: Bearer <key> you issued. For enterprise
   customers who won't go through the marketplace, and for your testing.

RAPIDAPI_PROXY_SECRET is the single most important variable you will set.
Without it, anyone who discovers your Railway URL uses the API for free,
because RapidAPI's metering happens on their side, not yours.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_session

logger = logging.getLogger(__name__)

# Maps RapidAPI's subscription header onto the plan names you create in
# their Pricing tab. Keep in sync with docs/RAPIDAPI_LISTING.md.
_PLAN_ALIASES = {
    "BASIC": "BASIC", "FREE": "BASIC",
    "PRO": "PRO",
    "ULTRA": "TEAM", "TEAM": "TEAM",
    "MEGA": "ENTERPRISE", "ENTERPRISE": "ENTERPRISE", "CUSTOM": "ENTERPRISE",
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    external_id: str
    plan: str
    source: str  # rapidapi | direct | dev

    @property
    def quota(self) -> int:
        return settings.plan_quotas.get(self.plan, settings.plan_quotas["BASIC"])


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _upsert_tenant(
    session: AsyncSession, external_id: str, source: str, plan: str
) -> str:
    """Get-or-create. RapidAPI never tells us about a new subscriber ahead
    of time - the first request they make is the signup event."""
    row = (
        await session.execute(
            text(
                "INSERT INTO tenants (external_id, source, plan) "
                "VALUES (:eid, :src, :plan) "
                "ON CONFLICT (external_id) DO UPDATE "
                "  SET plan = EXCLUDED.plan, updated_at = now() "
                "RETURNING id"
            ),
            {"eid": external_id, "src": source, "plan": plan},
        )
    ).first()
    return str(row[0])


async def resolve_principal(
    x_rapidapi_user: str | None = Header(default=None, alias="X-RapidAPI-User"),
    x_rapidapi_subscription: str | None = Header(
        default=None, alias="X-RapidAPI-Subscription"
    ),
    x_rapidapi_proxy_secret: str | None = Header(
        default=None, alias="X-RapidAPI-Proxy-Secret"
    ),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    # ---- path 1: RapidAPI ------------------------------------------------
    if x_rapidapi_user:
        if settings.rapidapi_proxy_secret:
            if x_rapidapi_proxy_secret != settings.rapidapi_proxy_secret:
                logger.warning("rejected request with bad proxy secret")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid marketplace proxy signature.",
                )
        elif settings.is_production:
            logger.error("RAPIDAPI_PROXY_SECRET is not set in production")

        plan = _PLAN_ALIASES.get((x_rapidapi_subscription or "BASIC").upper(), "BASIC")
        tenant_id = await _upsert_tenant(
            session, f"rapidapi:{x_rapidapi_user}", "rapidapi", plan
        )
        return Principal(tenant_id, x_rapidapi_user, plan, "rapidapi")

    # ---- path 2: direct bearer key --------------------------------------
    if authorization and settings.allow_direct_api_keys:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authorization header must be 'Bearer <api-key>'.",
            )
        digest = hash_key(raw.strip())
        row = (
            await session.execute(
                text(
                    "SELECT t.id, t.external_id, t.plan FROM api_keys k "
                    "JOIN tenants t ON t.id = k.tenant_id "
                    "WHERE k.key_hash = :h AND k.revoked_at IS NULL"
                ),
                {"h": digest},
            )
        ).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown API key."
            )
        await session.execute(
            text("UPDATE api_keys SET last_used_at = now() WHERE key_hash = :h"),
            {"h": digest},
        )
        return Principal(str(row[0]), row[1], row[2], "direct")

    # ---- path 3: local development only ---------------------------------
    if settings.allow_anonymous_dev_tenant and not settings.is_production:
        tenant_id = await _upsert_tenant(session, "dev:anonymous", "dev", "ENTERPRISE")
        return Principal(tenant_id, "dev:anonymous", "ENTERPRISE", "dev")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Missing credentials. Call this API through RapidAPI, or send "
            "'Authorization: Bearer <api-key>'."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )
