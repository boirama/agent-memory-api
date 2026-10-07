"""/v1/billing - usage visibility.

RapidAPI charges the customer; this endpoint tells the customer (and you)
what they actually consumed and what it cost to serve. Exposing it publicly
is a trust signal that costs nothing and differentiates you from competitors
who keep usage opaque until the invoice lands.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .. import metering
from ..auth import Principal, resolve_principal
from ..db import get_session
from ..schemas import OperationUsage, UsageResponse

router = APIRouter(prefix="/v1/billing", tags=["Billing"])


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="Check your current usage and remaining quota",
    description=(
        "Calls used this billing period broken down by operation, the included "
        "allowance for your plan, and any overage. Poll this to warn users "
        "before they hit a limit rather than after."
    ),
)
async def get_usage(
    principal: Principal = Depends(resolve_principal),
    session: AsyncSession = Depends(get_session),
) -> UsageResponse:
    data = await metering.summary(session, principal)
    return UsageResponse(
        billing_period=data["billing_period"], plan=data["plan"],
        total_calls=data["total_calls"], included_calls=data["included_calls"],
        remaining_calls=data["remaining_calls"], overage_calls=data["overage_calls"],
        estimated_cost_usd=data["estimated_cost_usd"],
        by_operation=[OperationUsage(**o) for o in data["by_operation"]],
        period_start=data["period_start"], period_end=data["period_end"],
    )
