# RapidAPI listing copy

Paste into the **About** tab in Phase 6, Step 6.

---

## Short description (Phase 6, Step 2)

> Persistent memory for AI agents. Store what your agent learns, recall it with semantic search, and share context between agents with scoped permissions.

Category: **Artificial Intelligence**

---

## Long description (About tab)

### Make your AI agent remember context across sessions

Every AI agent forgets everything when the conversation ends. Users re-answer
the same onboarding questions. Two of your agents need the same customer
context but cannot see each other's data. Stale facts pollute responses long
after they stopped being true.

Agent Memory API is a REST service that fixes all three.

### Three calls to working memory

```python
import httpx

BASE = "https://agent-memory-api.p.rapidapi.com"
H = {"X-RapidAPI-Key": "YOUR_KEY",
     "X-RapidAPI-Host": "agent-memory-api.p.rapidapi.com"}

# 1. Register your agent (idempotent, call on boot)
httpx.post(f"{BASE}/v1/agents", headers=H, json={"agent_id": "support-bot-001"})

# 2. Store what it learned
httpx.post(f"{BASE}/v1/memories", headers=H, json={
    "content": "User prefers email notifications over SMS",
    "agent_id": "support-bot-001",
    "namespace": "acme-corp/users/preferences",
    "ttl_seconds": 2592000,
})

# 3. Recall it next session
r = httpx.get(f"{BASE}/v1/memories", headers=H, params={
    "query": "how does this user want to be contacted",
    "agent_id": "support-bot-001",
})
# -> "User prefers email notifications over SMS"  (score 0.87)
```

### What makes recall different

Most memory APIs rank by vector distance alone, which treats every stored
fact as equally valuable forever. This one scores three signals at once:

| Signal | Weight | Measures |
|---|---|---|
| Semantic | 0.55 | Cosine similarity to your query |
| Lexical | 0.25 | Full-text match, so exact names and IDs still hit |
| Salience | 0.20 | Importance decayed by time, reinforced by recall frequency |

A fact recalled forty times is load-bearing and gets stronger. A fact nobody
has touched in six months fades. Your agent's working set sharpens instead of
drowning in stale context. Every result returns its individual sub-scores, so
you can see exactly why something ranked where it did.

### Agent-to-agent permissions

Agents grant each other scoped access to memory namespaces, like sharing a
folder. A sales agent's notes on a lead become readable by the support agent
handling the ticket, without either one owning the other's data.

Grants are hierarchical — a grant on `acme/sales` also covers
`acme/sales/leads`. They are revocable instantly and can auto-expire.

### Also included

- **Automatic expiry.** Set `ttl_seconds` and stale facts remove themselves.
- **Deduplication.** Re-storing a near-identical fact updates it instead of
  creating a rival copy, so recall never has to choose between contradictions.
- **Cost transparency.** Every response includes `cost_usd`.
- **MCP server.** Works natively in Claude Desktop, Claude Code, Cursor and
  Windsurf with no integration code.
- **Soft and hard delete.** Hard delete for GDPR erasure requests.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/memories` | Store a memory |
| GET | `/v1/memories` | Recall with semantic search |
| GET | `/v1/memories/{memory_id}` | Fetch one memory |
| DELETE | `/v1/memories/{memory_id}` | Forget a memory |
| POST | `/v1/agents` | Register an agent |
| GET | `/v1/agents` | List agents |
| POST | `/v1/permissions` | Grant scoped access |
| GET | `/v1/permissions` | List active grants |
| DELETE | `/v1/permissions/{permission_id}` | Revoke access |
| GET | `/v1/stats` | Memory statistics |
| GET | `/v1/billing/usage` | Usage and quota |

### Authentication

Send `X-RapidAPI-Key` and `X-RapidAPI-Host` on every request. Keys are
server-side credentials — never ship one in browser JavaScript or a mobile
bundle.

### Full documentation

https://YOUR-GITHUB-USERNAME.github.io/agent-memory-api/

---

## CORRECTED endpoint table for Phase 6, Step 4

> **The table in your deployment guide is out of date.** It lists verb-style
> paths (`/v1/memory/store`, `/v1/mesh/share`). The API now uses REST nouns.
> Enter these instead.

| Name | Method | Path |
|---|---|---|
| Store Memory | POST | `/v1/memories` |
| Recall Memories | GET | `/v1/memories` |
| Get Memory | GET | `/v1/memories/{memory_id}` |
| Forget Memory | DELETE | `/v1/memories/{memory_id}` |
| Register Agent | POST | `/v1/agents` |
| List Agents | GET | `/v1/agents` |
| Grant Permission | POST | `/v1/permissions` |
| List Permissions | GET | `/v1/permissions` |
| Revoke Permission | DELETE | `/v1/permissions/{permission_id}` |
| Memory Stats | GET | `/v1/stats` |
| Usage | GET | `/v1/billing/usage` |

**Faster alternative:** RapidAPI can import an OpenAPI spec directly instead
of you typing eleven endpoints by hand. Upload `site/openapi.json` — it
carries every parameter, response code and description already.

---

## Pricing plans (Phase 6, Step 5)

Unchanged from your deployment guide:

| Plan | Price | Requests/mo | Overage |
|---|---|---|---|
| Basic | Free | 1,000 | — |
| Pro | $49/mo | 50,000 | $0.001/req |
| Team | $149/mo | 200,000 | $0.0008/req |
| Enterprise | Custom | Unlimited | — |

These match `PLAN_QUOTAS` in `app/config.py`. If you change them on RapidAPI,
change them there too, or your in-API quota enforcement will disagree with
what the customer was sold.

**Check your margin before launching.** At the default `hashing` embedder
your marginal cost is essentially storage. Switch to `EMBEDDING_PROVIDER=openai`
and each store costs roughly $0.00002 in embeddings — about 2% of the $0.001
overage price, so the margin holds comfortably. Recalls embed the query too,
so they carry the same per-call embedding cost.
