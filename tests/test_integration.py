"""End-to-end tests against a real Postgres with pgvector.

Integration tests on purpose. The interesting behaviour here - hybrid
ranking, permission visibility, dedupe, TTL expiry, score filtering - lives
in SQL, so mocking the database would test nothing worth testing.

    pytest tests/ -v

Point TEST_DATABASE_URL at any pgvector-enabled Postgres, or leave it unset
to use the docker-compose instance.
"""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ALLOW_ANONYMOUS_DEV_TENANT", "true")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/agentmemory",
    ),
)

from app.main import app  # noqa: E402

SALES = "sales-bot-001"
SUPPORT = "support-bot-001"
NS = "acme-corp/sales/leads"


@pytest.fixture(scope="module", autouse=True)
def clean_database():
    """Start every run from an empty database.

    These tests assert on absolute counts ("this agent sees 0 memories before
    a grant exists"), so leftover rows from a previous run make them fail for
    reasons that have nothing to do with the code. Truncating is the honest
    fix; making the assertions relative would just hide state bugs.
    """
    import asyncio

    from sqlalchemy import text

    from app.db import engine

    async def _truncate():
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE mesh_shares, mesh_grants, usage_events, memories, "
                    "agents, api_keys, tenants RESTART IDENTITY CASCADE"
                )
            )

    asyncio.run(_truncate())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------- system ------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "ok"


def test_schema_applied(client):
    r = client.get("/health/schema")
    assert r.json()["migrations_applied"], r.json()["missing_tables"]


def test_llms_txt_served(client):
    r = client.get("/llms.txt")
    assert r.status_code == 200
    body = r.text
    assert "# Agent Memory API" in body
    assert "POST /v1/memories" in body


def test_openapi_has_problem_first_summaries(client):
    spec = client.get("/openapi.json").json()
    # The AI-discoverability framework hinges on these fields being written
    # as problems, not endpoint restatements.
    summary = spec["paths"]["/v1/memories"]["post"]["summary"]
    assert "memory" in summary.lower()
    assert summary != "Create Memory"
    assert spec["paths"]["/v1/memories"]["get"]["description"]


# ------------------------------------------------------------- memory ------


def test_store_and_recall(client):
    r = client.post(
        "/v1/memories",
        json={
            "content": "Dana Whitfield is the procurement lead at Acme and always prefers email over phone calls.",
            "agent_id": SALES,
            "namespace": NS,
            "scope": "user",
            "subject_id": "lead-acme-001",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "created"
    # "always" and "prefers" are high-signal, so inferred importance should
    # land above the 0.5 baseline.
    assert body["importance"] > 0.5

    r = client.get(
        "/v1/memories",
        params={"query": "who is the procurement contact at Acme", "agent_id": SALES},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "expected at least one recalled memory"
    assert "Dana Whitfield" in results[0]["content"]
    assert results[0]["via"] == "owned"


def test_fetch_one_memory(client):
    created = client.post(
        "/v1/memories",
        json={"content": "Acme uses Coupa for all invoice submission.",
              "agent_id": SALES, "namespace": NS, "dedupe": False},
    ).json()

    r = client.get(
        f"/v1/memories/{created['memory_id']}", params={"agent_id": SALES}
    )
    assert r.status_code == 200
    assert r.json()["memory_id"] == created["memory_id"]


def test_fetch_rejects_agent_without_access(client):
    created = client.post(
        "/v1/memories",
        json={"content": "Private note only the sales bot should read.",
              "agent_id": SALES, "namespace": "acme-corp/private", "dedupe": False},
    ).json()

    r = client.get(
        f"/v1/memories/{created['memory_id']}", params={"agent_id": SUPPORT}
    )
    assert r.status_code == 404


def test_dedupe_updates_instead_of_duplicating(client):
    payload = {
        "content": "Acme's fiscal year ends on March 31 and budget approvals freeze in February.",
        "agent_id": SALES,
        "namespace": NS,
    }
    first = client.post("/v1/memories", json=payload).json()
    second = client.post("/v1/memories", json=payload).json()

    assert second["status"] == "updated"
    assert second["memory_id"] == first["memory_id"]


def test_importance_inference_ranks_signal_above_noise():
    from app.ranking import infer_importance

    high = infer_importance("The contract requires a 30-day notice period. Deadline 2026-11-01.")
    low = infer_importance("maybe we should test this later?")
    assert high > low
    assert 0.0 <= low <= high <= 1.0


def test_ttl_expiry_excluded_from_recall(client):
    client.post(
        "/v1/memories",
        json={
            "content": "Temporary scratch note about widget pricing experiment zulu.",
            "agent_id": SALES,
            "namespace": "acme-corp/scratch",
            "ttl_seconds": 1,
            "dedupe": False,
        },
    )
    time.sleep(1.5)
    r = client.get(
        "/v1/memories",
        params={"query": "widget pricing experiment zulu", "agent_id": SALES},
    )
    contents = [m["content"] for m in r.json()["results"]]
    assert not any("zulu" in c for c in contents), "expired memory was returned"


def test_forget(client):
    stored = client.post(
        "/v1/memories",
        json={"content": "Disposable fact about quarterly offsite logistics yankee.",
              "agent_id": SALES, "dedupe": False},
    ).json()

    r = client.delete(
        f"/v1/memories/{stored['memory_id']}", params={"agent_id": SALES}
    )
    assert r.json()["status"] == "deleted"

    r = client.get(
        "/v1/memories",
        params={"query": "quarterly offsite logistics yankee", "agent_id": SALES},
    )
    assert not any(m["memory_id"] == stored["memory_id"] for m in r.json()["results"])


def test_forget_rejects_non_owner(client):
    stored = client.post(
        "/v1/memories",
        json={"content": "Owned only by the sales agent.", "agent_id": SALES,
              "dedupe": False},
    ).json()

    r = client.delete(
        f"/v1/memories/{stored['memory_id']}", params={"agent_id": SUPPORT}
    )
    assert r.json()["status"] == "not_found"


# --------------------------------------------------------- score floor -----


def test_min_score_filters_weak_matches(client):
    """A score floor must cut irrelevant results, not just low-importance ones.

    Without this, an agent asking an unrelated question still gets handed
    whatever happens to be in the namespace, and that noise goes straight
    into a prompt.
    """
    for content, ns in [
        ("Sean prefers email notifications over SMS", "score/preferences"),
        ("The deployment runs on Railway in the Frankfurt region", "score/infra"),
        ("The support SLA is 4 hours for P1 tickets", "score/support"),
    ]:
        client.post("/v1/memories", json={
            "content": content, "agent_id": "score-bot",
            "namespace": ns, "dedupe": False,
        })

    unfiltered = client.get("/v1/memories", params={
        "query": "notification preferences", "agent_id": "score-bot",
        "namespace": "score", "limit": 10,
    }).json()

    assert unfiltered["count"] >= 2, "need competing results to test filtering"
    assert "prefers email" in unfiltered["results"][0]["content"]

    # A floor midway between best and second-best: exactly one result should
    # survive. A hard-coded constant would be brittle against weight changes.
    top = unfiltered["results"][0]["score"]
    second = unfiltered["results"][1]["score"]
    assert top > second, "expected a clear winner to set a floor between"
    floor = round((top + second) / 2, 6)

    filtered = client.get("/v1/memories", params={
        "query": "notification preferences", "agent_id": "score-bot",
        "namespace": "score", "limit": 10, "min_score": floor,
    }).json()

    assert filtered["count"] == 1
    assert "prefers email" in filtered["results"][0]["content"]
    assert all(m["score"] >= floor for m in filtered["results"])


def test_min_score_can_return_nothing(client):
    """Returning zero results beats returning noise."""
    client.post("/v1/memories", json={
        "content": "A note about garden furniture deliveries",
        "agent_id": "score-bot", "namespace": "score/misc", "dedupe": False,
    })
    r = client.get("/v1/memories", params={
        "query": "quantum chromodynamics lattice gauge theory",
        "agent_id": "score-bot", "namespace": "score", "min_score": 0.5,
    })
    assert r.status_code == 200
    assert r.json()["count"] == 0, "weak matches leaked past the score floor"


def test_min_score_default_is_backwards_compatible(client):
    """Omitting min_score must behave exactly as before it existed."""
    client.post("/v1/memories", json={
        "content": "Baseline memory for the default min_score check",
        "agent_id": "compat-bot", "namespace": "compat", "dedupe": False,
    })
    without = client.get("/v1/memories", params={
        "query": "baseline memory", "agent_id": "compat-bot",
    }).json()
    with_zero = client.get("/v1/memories", params={
        "query": "baseline memory", "agent_id": "compat-bot", "min_score": 0,
    }).json()

    assert without["count"] == with_zero["count"] >= 1


# -------------------------------------------------------- permissions ------


def test_agents_register_idempotently(client):
    first = client.post(
        "/v1/agents", json={"agent_id": SALES, "framework": "custom"}
    ).json()
    second = client.post(
        "/v1/agents", json={"agent_id": SALES, "framework": "custom"}
    ).json()
    assert first["status"] == "registered"
    assert second["status"] == "updated"

    client.post("/v1/agents", json={"agent_id": SUPPORT, "framework": "langchain"})
    listed = client.get("/v1/agents").json()
    assert {a["agent_id"] for a in listed} >= {SALES, SUPPORT}


def test_isolation_before_grant(client):
    r = client.get(
        "/v1/memories",
        params={"query": "procurement contact Acme", "agent_id": SUPPORT},
    )
    assert r.json()["count"] == 0, "agent saw memories before any grant existed"


def test_hierarchical_namespace_grant(client):
    grant = client.post(
        "/v1/permissions",
        json={
            "granter_agent_id": SALES,
            "grantee_agent_id": SUPPORT,
            # Grant on the PARENT namespace; memories live in the child.
            "namespace": "acme-corp/sales",
            "permission": "read",
        },
    )
    assert grant.status_code == 201, grant.text
    assert grant.json()["kind"] == "namespace"

    r = client.get(
        "/v1/memories",
        params={"query": "procurement contact Acme", "agent_id": SUPPORT},
    )
    results = r.json()["results"]
    assert results, "hierarchical grant did not expose child namespace"
    assert results[0]["via"] == "grant"
    assert SALES in r.json()["agents_consulted"]


def test_include_shared_false_excludes_granted(client):
    r = client.get(
        "/v1/memories",
        params={
            "query": "procurement contact Acme",
            "agent_id": SUPPORT,
            "include_shared": "false",
        },
    )
    assert r.json()["count"] == 0


def test_list_permissions(client):
    perms = client.get("/v1/permissions", params={"agent_id": SUPPORT}).json()
    assert any(p["kind"] == "namespace" and p["grantee_agent_id"] == SUPPORT for p in perms)


def test_revoke_removes_access(client):
    grant = client.post(
        "/v1/permissions",
        json={
            "granter_agent_id": SALES,
            "grantee_agent_id": SUPPORT,
            "namespace": "acme-corp/sales",
            "permission": "read",
        },
    ).json()

    revoked = client.delete(f"/v1/permissions/{grant['permission_id']}")
    assert revoked.json()["status"] == "revoked"

    r = client.get(
        "/v1/memories",
        params={"query": "procurement contact Acme", "agent_id": SUPPORT},
    )
    assert r.json()["count"] == 0, "revoked grant still allowed access"


def test_single_memory_share(client):
    stored = client.post(
        "/v1/memories",
        json={"content": "Escalation contact for Acme outages is the on-call SRE.",
              "agent_id": SALES, "namespace": "acme-corp/ops", "dedupe": False},
    ).json()

    share = client.post(
        "/v1/permissions",
        json={
            "granter_agent_id": SALES,
            "grantee_agent_id": SUPPORT,
            "memory_id": stored["memory_id"],
            "permission": "read",
        },
    )
    assert share.status_code == 201
    assert share.json()["kind"] == "memory"

    r = client.get(
        "/v1/memories",
        params={"query": "escalation contact for outages", "agent_id": SUPPORT},
    )
    hits = [m for m in r.json()["results"] if m["memory_id"] == stored["memory_id"]]
    assert hits, "direct share was not visible"
    assert hits[0]["via"] == "share"


def test_permission_requires_exactly_one_target(client):
    r = client.post(
        "/v1/permissions",
        json={"granter_agent_id": SALES, "grantee_agent_id": SUPPORT},
    )
    assert r.status_code == 400


def test_permission_rejects_self_grant(client):
    r = client.post(
        "/v1/permissions",
        json={"granter_agent_id": SALES, "grantee_agent_id": SALES,
              "namespace": "acme-corp/sales"},
    )
    assert r.status_code == 400


# ------------------------------------------------- stats, billing, errors --


def test_stats(client):
    r = client.get("/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_memories"] > 0
    assert body["total_agents"] >= 2
    assert any(n["namespace"] == NS for n in body["namespaces"])


def test_billing_usage(client):
    r = client.get("/v1/billing/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] > 0
    assert {"store", "recall"} <= {o["operation"] for o in body["by_operation"]}


def test_validation_errors_are_flat(client):
    r = client.post("/v1/memories", json={"content": "", "agent_id": SALES})
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"
    assert isinstance(r.json()["detail"], list)


def test_min_score_rejects_out_of_range(client):
    r = client.get("/v1/memories", params={
        "query": "anything", "agent_id": SALES, "min_score": 1.5,
    })
    assert r.status_code == 422
