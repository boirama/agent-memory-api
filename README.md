# Agent Memory API

Persistent, shareable memory for AI agents. Store what an agent learns, recall
it with hybrid semantic search, expire stale facts automatically, and grant
other agents scoped read access.

**Status:** 24 integration tests passing against real Postgres + pgvector.
6/7 top-1 recall accuracy, p50 latency 6.3 ms. See `docs/BENCHMARK.md`.

## Deployment order

Follow `api_deployment_phases.docx`, with the corrections in
`docs/DEPLOY_ADDENDUM.md`. Two things that document does **not** cover and
that matter:

1. **`RAPIDAPI_PROXY_SECRET`** — without it anyone who finds your Railway URL
   uses the API for free. Set it after Phase 6.
2. **Phase 9 (GitHub Pages)** and **Phase 10 (embedding provider)** — both in
   the addendum.

## Layout

```
app/                  FastAPI service
  config.py           settings, all env-overridable
  db.py               async engine (handles Supabase's pooler)
  cache.py            Redis, never load-bearing
  embeddings.py       hashing (default) and OpenAI providers
  ranking.py          hybrid scoring + visibility SQL
  metering.py         usage tracking and quota enforcement
  auth.py             RapidAPI headers and direct bearer keys
  routers/            memories, agents, permissions, billing, health
migrations/           three numbered SQL files for Supabase
site/                 docs site → GitHub Pages (Phase 9)
  index.html          self-contained quickstart + WebAPI schema
  guides/             four use-case pages with TechArticle schema
  llms.txt            AI-readable documentation index
  openapi.json        exported spec
mcp/server.py         MCP server for Claude, Cursor, Windsurf
docs/                 listing copy, launch copy, addendum, benchmark
scripts/              OpenAPI export
tests/                24 integration tests
worker.js             Cloudflare Worker edge proxy
```

## Local development

```bash
docker compose up -d          # Postgres+pgvector and Redis; migrations auto-run
pip install -r requirements.txt
uvicorn app.main:app --reload
open http://localhost:8000/docs
```

`docker-compose.yml` sets `ALLOW_ANONYMOUS_DEV_TENANT=true` so you can call
endpoints with no auth header locally. Never set that in production.

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Point `TEST_DATABASE_URL` at any pgvector-enabled Postgres. The suite
truncates tables in a fixture, so it is repeatable.

## Reproduce the benchmark

```bash
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/agentmemory"
export ALLOW_ANONYMOUS_DEV_TENANT=true CACHE_ENABLED=false
python - <<'PY'
from fastapi.testclient import TestClient
from app.main import app
import time, statistics
FACTS = [
 "Northwind requires all invoices to be submitted through Coupa, never by email.",
 "Northwind's renewal date is 2027-01-15 and they always negotiate hard in December.",
 "The support SLA for Northwind is 4 hours for P1 and 24 hours for P2 tickets.",
 "The last outage on 2026-06-02 was caused by a Redis connection leak.",
]
with TestClient(app) as c:
    for f in FACTS:
        c.post("/v1/memories", json={"content": f, "agent_id": "bench",
                                     "namespace": "bench", "dedupe": False})
    for q in ["how should we send invoices", "when does the contract renew",
              "what caused the outage"]:
        t = time.perf_counter()
        r = c.get("/v1/memories", params={"query": q, "agent_id": "bench",
                                          "namespace": "bench", "limit": 3})
        ms = (time.perf_counter() - t) * 1000
        print(f"{ms:5.1f}ms  {q!r}\n         {r.json()['results'][0]['content'][:60]}")
PY
```

## Regenerate the OpenAPI spec

```bash
python scripts/export_openapi.py
```

Run this after any endpoint change and commit `site/openapi.json`. The script
fails loudly if an operation is missing a description — AI agents match on
those fields.

## Known limitation

The default `hashing` embedder matches vocabulary, not meaning. "SLA" and
"response time commitment" score near zero together. Switch to
`EMBEDDING_PROVIDER=openai` before taking real money — and do it before you
have customer data, because existing memories keep their old vectors.
