"""FastAPI application entrypoint.

Local:   uvicorn app.main:app --reload
Railway: the Dockerfile's CMD does it for you.

The OpenAPI description below is doing double duty as marketing copy. It is
the first thing an AI agent reads when deciding whether this API answers a
developer's question, so it leads with the problem, not the endpoints.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import cache, db
from .config import settings
from .routers import agents, billing, health, memories, permissions

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("agent-memory-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting up (environment=%s)", settings.environment)
    await cache.init()

    if await db.ping():
        missing = await db.verify_schema()
        if missing:
            # Not fatal - the API boots and /health tells you what to fix,
            # which beats a crash loop on your first deploy.
            logger.error(
                "MIGRATIONS NOT APPLIED - missing tables: %s. Run the files in "
                "migrations/ in the Supabase SQL Editor.",
                ", ".join(missing),
            )
        else:
            logger.info("schema verified")
    else:
        logger.error("database unreachable - check DATABASE_URL")

    yield

    logger.info("shutting down")
    await cache.close()
    await db.dispose()


DESCRIPTION = """
**Make your AI agent remember context across sessions.**

Every AI agent forgets everything when the conversation ends. This API gives
agents somewhere to put what they learn, and a way to get it back ranked by
how relevant *and* how important it is.

### What it solves

- Your agent asks a user the same onboarding questions every session
- Two agents need the same customer context but can't see each other's data
- Stale facts pollute responses long after they stopped being true
- You need memory recall fast enough to sit in a live conversation

### How recall works

Every result is scored on three signals at once: semantic similarity,
full-text match, and salience. Salience decays for memories nobody touches
and strengthens for memories recalled often, so an agent's working set
sharpens over time instead of drowning in stale facts.

### Sharing between agents

Agents grant each other scoped access to namespaces, like sharing a folder.
A sales agent's notes on a lead become readable by the support agent handling
the ticket, without either one owning the other's data. Grants are
hierarchical, revocable and time-boxable.

### Three calls to get started

1. `POST /v1/agents` — register your agent (idempotent, call it on boot)
2. `POST /v1/memories` — store a fact
3. `GET /v1/memories?query=...&agent_id=...` — get it back

Every response includes `cost_usd`, so you always know what a call cost.
"""

app = FastAPI(
    title="Agent Memory API",
    version="1.0.0",
    summary="Persistent, shareable memory for AI agents with semantic recall and TTL expiry.",
    description=DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {
            "name": "Memory",
            "description": "Store, recall, and forget agent memories with semantic search.",
        },
        {
            "name": "Agents",
            "description": "Register the agents that own and share memory.",
        },
        {
            "name": "Permissions",
            "description": "Grant and revoke agent-to-agent access to memory namespaces.",
        },
        {"name": "Billing", "description": "Usage and quota visibility."},
        {"name": "System", "description": "Health checks and discovery."},
    ],
    license_info={"name": "Proprietary"},
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Response-Time-Ms"],
)


@app.middleware("http")
async def observability(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
    # Point AI crawlers that hit the API host at the human docs.
    response.headers["Link"] = f'<{settings.docs_base_url}>; rel="describedby"'

    if request.url.path not in ("/health", "/"):
        logger.info(
            "%s %s -> %d in %.1fms",
            request.method, request.url.path, response.status_code, elapsed_ms,
        )
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default validation body is nested and awkward to read from an
    # agent's tool-call error handler. Flatten it.
    problems = [
        {
            "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
            "problem": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "validation_error", "detail": problems},
    )


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": _slug(exc.status_code), "detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": str(exc) if not settings.is_production else "An unexpected error occurred.",
        },
    )


def _slug(code: int) -> str:
    return {
        400: "bad_request", 401: "unauthorized", 403: "forbidden",
        404: "not_found", 409: "conflict", 413: "payload_too_large",
        422: "validation_error", 429: "rate_limited",
    }.get(code, "error")


app.include_router(health.router)
app.include_router(memories.router)
app.include_router(memories.stats_router)
app.include_router(agents.router)
app.include_router(permissions.router)
app.include_router(billing.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, log_level="info")
