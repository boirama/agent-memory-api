"""Request and response models.

These double as your documentation. FastAPI generates the OpenAPI spec from
them, RapidAPI imports that spec directly, and AI crawlers read the
`summary` and `description` fields for semantic matching. So the prose here
is load-bearing marketing, not just type hints - write it in the language a
developer would use to search for this problem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Scope = Literal["user", "agent", "session", "org"]
Permission = Literal["read", "write", "admin"]


# ---------------------------------------------------------------- memory ----


class CreateMemoryRequest(BaseModel):
    content: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="The fact, preference or observation to remember.",
        examples=["User prefers email notifications over SMS"],
    )
    agent_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Identifier of the agent writing this memory.",
        examples=["support-bot-001"],
    )
    scope: Scope = Field(
        default="agent",
        description=(
            "user = a fact about a person, agent = the agent's own knowledge, "
            "session = ephemeral, org = shared organisation-wide."
        ),
    )
    namespace: str = Field(
        default="default",
        max_length=256,
        description="Slash-delimited grouping. Permissions are granted per namespace.",
        examples=["acme-corp/users/preferences"],
    )
    subject_id: str | None = Field(
        default=None,
        max_length=128,
        description="Who or what this memory is about (a user id, a session id).",
        examples=["user_8f3kLm92Xn"],
    )
    importance: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Base salience 0-1. Omit and it is inferred from the content.",
        examples=[0.85],
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=0,
        le=315_360_000,
        description="Auto-expire after this many seconds. Omit or 0 for permanent.",
        examples=[2592000],
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    dedupe: bool = Field(
        default=True,
        description=(
            "If a near-identical memory already exists in this namespace, "
            "update it instead of creating a duplicate."
        ),
    )

    @field_validator("namespace")
    @classmethod
    def _clean_namespace(cls, v: str) -> str:
        return v.strip().strip("/") or "default"


class MemoryRecord(BaseModel):
    memory_id: str
    content: str
    agent_id: str
    scope: Scope
    namespace: str
    subject_id: str | None = None
    importance: float
    effective_importance: float | None = None
    access_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateMemoryResponse(BaseModel):
    memory_id: str
    status: Literal["created", "updated"] = "created"
    namespace: str
    importance: float
    expires_at: datetime | None = None
    tokens_used: int
    cost_usd: float


class RecalledMemory(MemoryRecord):
    score: float = Field(description="Final hybrid relevance score, 0-1.")
    semantic_score: float = Field(description="Cosine similarity to the query.")
    lexical_score: float = Field(description="Full-text match strength.")
    salience_score: float = Field(description="Importance after decay and reinforcement.")
    via: Literal["owned", "grant", "share"] = Field(
        default="owned",
        description="How this agent can see it: it owns it, a namespace grant, or a direct share.",
    )


class RecallResponse(BaseModel):
    query: str
    results: list[RecalledMemory]
    count: int
    agents_consulted: list[str] = Field(
        default_factory=list,
        description="Which agents contributed results, including shared ones.",
    )
    latency_ms: float
    cached: bool = False
    cost_usd: float


class DeleteMemoryResponse(BaseModel):
    memory_id: str
    status: Literal["deleted", "not_found"]
    hard_deleted: bool = False


class NamespaceStat(BaseModel):
    namespace: str
    count: int
    avg_importance: float


class StatsResponse(BaseModel):
    total_memories: int
    active_memories: int
    expired_memories: int
    total_agents: int
    namespaces: list[NamespaceStat]
    oldest_memory_at: datetime | None = None
    newest_memory_at: datetime | None = None
    storage_bytes: int


# ---------------------------------------------------------------- agents ----


class RegisterAgentRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, examples=["support-bot-001"])
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=2000)
    framework: str | None = Field(
        default=None,
        max_length=64,
        description="langchain | crewai | autogen | mcp | custom",
        examples=["langchain"],
    )
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    discoverable: bool = Field(
        default=False,
        description="Let other agents in your account find this one.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRecord(BaseModel):
    agent_id: str
    display_name: str | None = None
    description: str | None = None
    framework: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    discoverable: bool = False
    created_at: datetime
    last_seen_at: datetime


class RegisterAgentResponse(BaseModel):
    agent: AgentRecord
    status: Literal["registered", "updated"]


# ----------------------------------------------------------- permissions ----


class GrantPermissionRequest(BaseModel):
    granter_agent_id: str = Field(
        ..., min_length=1, max_length=128, description="The agent giving access."
    )
    grantee_agent_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The agent receiving access. Use '*' for every agent in your account.",
    )
    namespace: str | None = Field(
        default=None,
        max_length=256,
        description=(
            "Grant standing access to a namespace and its children - a grant on "
            "'acme/sales' also covers 'acme/sales/leads'. "
            "Mutually exclusive with memory_id."
        ),
        examples=["acme-corp/users/preferences"],
    )
    memory_id: str | None = Field(
        default=None,
        description="Share one specific memory instead. Mutually exclusive with namespace.",
    )
    permission: Permission = "read"
    ttl_seconds: int | None = Field(
        default=None, ge=0, le=315_360_000, description="Auto-revoke after this long."
    )
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("namespace")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return (v.strip().strip("/") or None) if v is not None else None


class PermissionRecord(BaseModel):
    permission_id: str
    kind: Literal["namespace", "memory"]
    granter_agent_id: str
    grantee_agent_id: str
    namespace: str | None = None
    memory_id: str | None = None
    permission: Permission
    expires_at: datetime | None = None
    created_at: datetime | None = None


class GrantPermissionResponse(PermissionRecord):
    cost_usd: float


class RevokePermissionResponse(BaseModel):
    permission_id: str
    status: Literal["revoked", "not_found"]


# --------------------------------------------------------------- billing ----


class OperationUsage(BaseModel):
    operation: str
    calls: int
    cost_usd: float


class UsageResponse(BaseModel):
    billing_period: str
    plan: str
    total_calls: int
    included_calls: int
    remaining_calls: int
    overage_calls: int
    estimated_cost_usd: float
    by_operation: list[OperationUsage]
    period_start: datetime
    period_end: datetime
