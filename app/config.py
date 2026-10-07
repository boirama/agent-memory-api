"""Configuration. Every value is settable via a Railway environment variable.

The deployment guide asks you to set five: DATABASE_URL, REDIS_URL,
SECRET_KEY, ENVIRONMENT, PORT. Everything else has a working default, so
the API boots with just those five.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- core ----------------------------------------------------------
    environment: Literal["development", "staging", "production"] = "development"
    port: int = 8000
    secret_key: str = "dev-only-insecure-key-set-SECRET_KEY-in-production"
    log_level: str = "INFO"

    # Where your docs live. Emitted in the OpenAPI spec and in the
    # Link headers AI crawlers follow. Set it once you publish GitHub Pages.
    docs_base_url: str = "https://example.github.io/agent-memory-api"

    # ---- datastores ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
    redis_url: str = "redis://localhost:6379/0"

    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout: int = 30
    db_command_timeout: int = 15

    # ---- embeddings ----------------------------------------------------
    # "hashing" needs no API key and no model download: it works the moment
    # you deploy, but matches vocabulary rather than meaning. "openai" is
    # meaningfully better at paraphrase. Switch before you take real money.
    embedding_provider: Literal["hashing", "openai"] = "hashing"
    embedding_dim: int = 768  # must match vector(768) in migration 001
    openai_api_key: str | None = None
    openai_embedding_model: str = "text-embedding-3-small"

    # ---- ranking -------------------------------------------------------
    weight_semantic: float = 0.55
    weight_lexical: float = 0.25
    weight_salience: float = 0.20
    decay_rate_per_day: float = 0.015  # ~46-day half-life when unaccessed
    reinforcement_factor: float = 0.06
    default_recall_limit: int = 10
    max_recall_limit: int = 100
    max_content_chars: int = 8000

    # ---- caching -------------------------------------------------------
    cache_recall_ttl: int = 30
    cache_stats_ttl: int = 60
    cache_enabled: bool = True

    # ---- auth ----------------------------------------------------------
    # Set this to the Proxy Secret from RapidAPI's Configuration tab.
    # Without it, anyone who finds your Railway URL uses the API free.
    rapidapi_proxy_secret: str | None = None
    allow_direct_api_keys: bool = True
    allow_anonymous_dev_tenant: bool = False

    # ---- metering (your unit economics) --------------------------------
    cost_per_store_usd: float = 0.0010
    cost_per_recall_usd: float = 0.0005
    cost_per_permission_usd: float = 0.0100

    plan_quotas: dict[str, int] = Field(
        default_factory=lambda: {
            "BASIC": 1_000,
            "PRO": 50_000,
            "TEAM": 200_000,
            "ENTERPRISE": 100_000_000,
        }
    )

    @field_validator("database_url")
    @classmethod
    def _force_asyncpg(cls, v: str) -> str:
        """Supabase hands you postgresql://. SQLAlchemy async needs the
        asyncpg driver named explicitly, so patch it rather than fail."""
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def uses_pgbouncer(self) -> bool:
        """Supabase's Supavisor pooler (:6543) can't do prepared statements."""
        return ":6543" in self.database_url or "pooler.supabase" in self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

if os.getenv("PORT"):
    try:
        settings.port = int(os.environ["PORT"])
    except ValueError:
        pass
