"""Redis cache (Upstash in production).

Design rule: the cache is never load-bearing. Every function swallows
connection errors and returns None/False, so if Upstash is down or you blow
the free tier's daily command limit, the API keeps serving from Postgres.
Slower, but alive.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from .config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None


def _build_client() -> aioredis.Redis:
    # rediss:// (Upstash) needs TLS; redis-py handles it from the scheme.
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        retry_on_timeout=True,
        health_check_interval=30,
    )


async def init() -> None:
    global _client
    if not settings.cache_enabled:
        return
    try:
        _client = _build_client()
        await _client.ping()
        logger.info("cache connected")
    except Exception as exc:  # noqa: BLE001
        _client = None
        logger.warning("cache unavailable, running degraded: %s", exc)


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
        _client = None


async def ping() -> bool:
    if _client is None:
        return False
    try:
        await _client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def key(*parts: Any) -> str:
    flat = ":".join(str(p) for p in parts)
    if len(flat) > 200:
        digest = hashlib.sha256(flat.encode()).hexdigest()[:32]
        return f"amem:{flat[:60]}:{digest}"
    return f"amem:{flat}"


async def get_json(k: str) -> Any | None:
    if _client is None:
        return None
    try:
        raw = await _client.get(k)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def set_json(k: str, value: Any, ttl: int) -> bool:
    if _client is None:
        return False
    try:
        await _client.set(k, json.dumps(value, default=str), ex=ttl)
        return True
    except Exception:  # noqa: BLE001
        return False


async def delete_prefix(prefix: str) -> int:
    """Invalidate by prefix using SCAN, not KEYS — KEYS blocks the server
    and Upstash will rate-limit you for it."""
    if _client is None:
        return 0
    removed = 0
    try:
        async for k in _client.scan_iter(match=f"{prefix}*", count=200):
            await _client.delete(k)
            removed += 1
    except Exception:  # noqa: BLE001
        pass
    return removed


async def incr_counter(k: str, ttl: int, amount: int = 1) -> int | None:
    if _client is None:
        return None
    try:
        pipe = _client.pipeline()
        pipe.incrby(k, amount)
        pipe.expire(k, ttl)
        result = await pipe.execute()
        return int(result[0])
    except Exception:  # noqa: BLE001
        return None


async def get_counter(k: str) -> int:
    if _client is None:
        return 0
    try:
        raw = await _client.get(k)
        return int(raw) if raw else 0
    except Exception:  # noqa: BLE001
        return 0
