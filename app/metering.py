"""Usage metering.

RapidAPI meters requests for billing, but you still need your own numbers:
theirs are per-request, yours need to be per-operation so you can tell
whether your $0.001 store price actually covers embedding plus storage.

Writes go to Postgres (durable, what GET /v1/billing/usage reads) and to a
Redis counter (fast, what quota checks read). If Redis is down, quota checks
fall back to Postgres rather than failing open - failing open on quota is how
you serve a million free calls to one enthusiastic user.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import cache
from .auth import Principal
from .config import settings

logger = logging.getLogger(__name__)

COST_TABLE = {
    "store": settings.cost_per_store_usd,
    "recall": settings.cost_per_recall_usd,
    "fetch": 0.0,
    "forget": 0.0,
    "permission": settings.cost_per_permission_usd,
    "register": 0.0,
    "stats": 0.0,
}


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _quota_key(tenant_id: str, period: str) -> str:
    return cache.key("quota", tenant_id, period)


async def record(
    session: AsyncSession,
    principal: Principal,
    operation: str,
    *,
    units: int = 1,
    agent_id: str | None = None,
) -> float:
    """Log one billable operation. Returns its cost in USD."""
    period = current_period()
    unit_cost = COST_TABLE.get(operation, 0.0)

    try:
        await session.execute(
            text(
                "INSERT INTO usage_events "
                "(tenant_id, operation, units, unit_cost_usd, agent_id, billing_period) "
                "VALUES (CAST(:tid AS uuid), :op, :units, :cost, :agent, :period)"
            ),
            {
                "tid": principal.tenant_id, "op": operation, "units": units,
                "cost": unit_cost, "agent": agent_id, "period": period,
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Never fail the user's request because the meter failed.
        logger.error("failed to record usage: %s", exc)

    # 40 days TTL so the counter outlives the period it tracks.
    await cache.incr_counter(
        _quota_key(principal.tenant_id, period), ttl=3_456_000, amount=units
    )
    return round(unit_cost * units, 8)


async def calls_this_period(session: AsyncSession, principal: Principal) -> int:
    period = current_period()
    cached = await cache.get_counter(_quota_key(principal.tenant_id, period))
    if cached:
        return cached

    row = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(units), 0) FROM usage_events "
                "WHERE tenant_id = CAST(:tid AS uuid) AND billing_period = :period"
            ),
            {"tid": principal.tenant_id, "period": period},
        )
    ).first()
    total = int(row[0]) if row else 0

    if total:
        await cache.incr_counter(
            _quota_key(principal.tenant_id, period), ttl=3_456_000, amount=total
        )
    return total


async def enforce_quota(session: AsyncSession, principal: Principal) -> None:
    """Second line of defence behind RapidAPI's own metering, which allows
    overage by default and is easy to misconfigure."""
    if principal.plan == "ENTERPRISE":
        return

    used = await calls_this_period(session, principal)
    limit = principal.quota

    # 10% headroom so a customer mid-upgrade isn't hard-stopped.
    if used >= int(limit * 1.1):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Monthly quota exhausted for plan {principal.plan} "
                f"({used}/{limit} calls). Upgrade your plan to continue."
            ),
            headers={"Retry-After": "3600", "X-Quota-Limit": str(limit)},
        )


async def summary(session: AsyncSession, principal: Principal) -> dict:
    period = current_period()
    rows = (
        await session.execute(
            text(
                "SELECT operation, COALESCE(SUM(units), 0) AS calls, "
                "       COALESCE(SUM(units * unit_cost_usd), 0) AS cost "
                "FROM usage_events "
                "WHERE tenant_id = CAST(:tid AS uuid) AND billing_period = :period "
                "GROUP BY operation ORDER BY calls DESC"
            ),
            {"tid": principal.tenant_id, "period": period},
        )
    ).all()

    by_operation = [
        {"operation": r[0], "calls": int(r[1]), "cost_usd": round(float(r[2]), 6)}
        for r in rows
    ]
    total_calls = sum(o["calls"] for o in by_operation)
    total_cost = round(sum(o["cost_usd"] for o in by_operation), 6)

    included = principal.quota
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        period_end = period_start.replace(year=now.year + 1, month=1)
    else:
        period_end = period_start.replace(month=now.month + 1)

    return {
        "billing_period": period,
        "plan": principal.plan,
        "total_calls": total_calls,
        "included_calls": included,
        "remaining_calls": max(0, included - total_calls),
        "overage_calls": max(0, total_calls - included),
        "estimated_cost_usd": total_cost,
        "by_operation": by_operation,
        "period_start": period_start,
        "period_end": period_end,
    }
