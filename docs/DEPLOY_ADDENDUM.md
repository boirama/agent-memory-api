# Deployment guide — corrections and additions

Your original `api_deployment_phases.docx` is still the right sequence.
These are the places it needs updating, plus two phases it doesn't cover.

---

## Phase 2 — repo name

Create the repo as `agent-memory-api` (unchanged). Push this whole folder,
including `site/` — GitHub Pages serves from it in the new Phase 9.

## Phase 3, Step 2 — migrations

Unchanged, but note two extensions were removed after live testing:

- `pgcrypto` — not needed. `gen_random_uuid()` has been Postgres core since 13.
- `pg_trgm` — was backing an index nothing queried.

The only extension required now is `vector`, which Supabase provides.

**Verify after running them.** Once the API is deployed, open
`https://YOUR-URL/health/schema`. It lists any missing table by name, which
beats guessing which of the three files failed.

## Phase 4, Step 2 — environment variables

The guide lists five. **Add these two.**

| Variable | Value | Why |
|---|---|---|
| `DATABASE_URL` | Supabase URI | as documented |
| `REDIS_URL` | Upstash URI | as documented |
| `SECRET_KEY` | long random string | as documented |
| `ENVIRONMENT` | `production` | as documented |
| `PORT` | `8000` | as documented |
| **`RAPIDAPI_PROXY_SECRET`** | from RapidAPI Configuration tab | **see below** |
| **`DOCS_BASE_URL`** | your GitHub Pages URL | emitted in the spec, `Link` header and `llms.txt` so AI crawlers find your guides from the API host |

### RAPIDAPI_PROXY_SECRET is not optional

RapidAPI meters and bills on *their* side. If someone finds your Railway URL —
and it is guessable, public, and appears in your Worker config — they can call
your API directly, forever, for free. This secret is the only thing that stops
that.

You cannot set it until Phase 6 exists, so the order is: deploy without it,
create the RapidAPI listing, copy the Proxy Secret from the Configuration tab,
then come back to Railway and add it. **Do this before you publish**, not
after.

While it is unset in production the API logs an error on every marketplace
request. That is deliberate.

### Supabase connection string

If you use the pooled connection (port `6543`, or a host containing
`pooler.supabase`), the app detects it and disables prepared statements
automatically. You don't need to configure anything — but if you see
`prepared statement _asyncpg_stmt_N does not exist` errors, that detection is
what handles it.

## Phase 5 — Cloudflare Worker

Unchanged. Edit `ORIGIN` at the top of `worker.js`, then `wrangler deploy`.

The Worker caches `GET /v1/memories` and `GET /v1/stats` for 20 seconds,
keyed by caller identity. Writes and billing are never cached.

## Phase 6, Step 4 — endpoint table has changed

**The table in the guide is out of date.** It lists verb-style paths
(`/v1/memory/store`, `/v1/mesh/share`). The API now uses REST nouns. The
corrected table is in `RAPIDAPI_LISTING.md`.

Better still: RapidAPI can import an OpenAPI spec. Upload `site/openapi.json`
instead of typing eleven endpoints by hand — it carries every parameter,
response code and description already.

## Phase 6, Step 6 — listing copy

Paste from `RAPIDAPI_LISTING.md`.

## Phase 7 — PayPal

Unchanged.

## Phase 8 — launch

Copy is in `LAUNCH.md`. One change of sequence: **post the dev.to article
first**, before Product Hunt. AI crawlers take days to weeks to index new
content, and being citable is the point.

---

# NEW — Phase 9: publish the docs site

The AI-discoverability work needs pages you control. You cannot add schema
markup or an `llms.txt` to your RapidAPI listing page, so that page cannot be
your discovery asset. GitHub Pages costs nothing and you already have the
account.

1. In your `agent-memory-api` repo, go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: `main`, folder: **`/site`**
4. Save. Your docs appear at
   `https://YOUR-USERNAME.github.io/agent-memory-api/` within a couple of minutes

### Then find and replace, across the whole `site/` folder

| Placeholder | Replace with |
|---|---|
| `YOUR-GITHUB-USERNAME` | your GitHub username |
| `YOUR-NAME` | your Cloudflare Workers subdomain |
| `YOUR-USERNAME` | your RapidAPI username |

These appear in `index.html`, all four guides, `llms.txt`, `robots.txt`,
`sitemap.xml` and `scripts/export_openapi.py`. In VS Code: Ctrl+Shift+H,
scope to `site/`.

### Verify it worked

- `https://YOUR-USERNAME.github.io/agent-memory-api/llms.txt` returns text
- `https://YOUR-USERNAME.github.io/agent-memory-api/openapi.json` returns JSON
- Paste the homepage into Google's Rich Results Test — the WebAPI block should parse
- `robots.txt` explicitly allows GPTBot, ClaudeBot and PerplexityBot

### Keep the spec fresh

Any time you change an endpoint:

```bash
python scripts/export_openapi.py
git add site/openapi.json && git commit -m "update spec" && git push
```

A stale spec is worse than none — an AI agent generating a call against a
parameter you removed produces a broken snippet with your name on it.

---

# NEW — Phase 10: switch the embedding provider

The API ships with `EMBEDDING_PROVIDER=hashing`. It needs no API key and works
the moment you deploy, which is why it is the default — but it matches
**vocabulary, not meaning**.

Measured on an 8-fact benchmark: 6/7 top-1 accuracy. The one miss was a query
asking about "response time commitment for urgent issues" against a memory
reading "support SLA is 4 hours for P1" — no shared words, so no match.

That is the failure mode your paying customers will hit.

**Before you take real money:**

1. Get an OpenAI API key
2. In Railway, set `EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY=sk-...`
3. Redeploy

Cost is about $0.00002 per store — roughly 2% of your $0.001 overage price.

**Important:** existing memories keep their old hashing-based vectors and will
not match new OpenAI-based query vectors properly. Switch before you have real
customer data, or re-embed everything afterwards. Doing it on day one is much
cheaper than doing it on day ninety.
