### Make your AI agent remember context across sessions

Every AI agent forgets everything when the conversation ends. Users re-answer
the same onboarding questions. Two of your agents need the same customer
context but cannot see each other's data. Stale facts pollute responses long
after they stopped being true.

Agent Memory API is a REST service that fixes all three.

### Three calls to working memory

```python
import httpx

BASE = "https://agent-memory-api1.p.rapidapi.com"
H = {"X-RapidAPI-Key": "YOUR_KEY",
     "X-RapidAPI-Host": "agent-memory-api1.p.rapidapi.com"}

# 1. Register your agent (idempotent, call on boot)
httpx.post(f"{BASE}/v1/agents", headers=H, json={"agent_id": "support-bot-001"})

# 2. Store what it learned
httpx.post(f"{BASE}/v1/memories", headers=H, json={
    "content": "User prefers email notifications over SMS",
    "agent_id": "support-bot-001",
    "namespace": "acme-corp/users/preferences",
    "ttl_seconds": 2592000,
})

# 3. Recall it next session - in different words
r = httpx.get(f"{BASE}/v1/memories", headers=H, params={
    "query": "how does this user want to be contacted",
    "agent_id": "support-bot-001",
    "min_score": 0.3,
})
# -> top result: "User prefers email notifications over SMS"
```

The query shares almost no words with the stored fact. Recall works on
meaning, not keyword overlap.

### How recall ranks results

Each memory is scored on three signals:

| Signal | Weight | Measures |
|---|---|---|
| Semantic | 0.80 | Meaning: cosine similarity between your query and the memory |
| Lexical | 0.15 | Full-text match, so exact names and IDs still hit |
| Salience | 0.05 | Importance, decayed by time since last use, reinforced by recall |

Meaning dominates, so an important but unrelated memory never outranks the
one that actually answers the question. Salience breaks ties: between two
equally relevant memories, the one your agent relies on wins, and facts nobody
has touched in months fade. Every result returns its sub-scores, so you can see
exactly why something ranked where it did.

### Keep irrelevant memories out of your prompt

Set `min_score` when you feed results straight into a prompt. **0.3 is a good
starting floor.** Raise it if unrelated memories slip through; lower it if real
answers go missing. Without a floor, an unrelated question still returns
whatever is in the namespace.

### Agent-to-agent permissions

Agents grant each other scoped access to memory namespaces, like sharing a
folder. A sales agent's notes on a lead become readable by the support agent
handling the ticket, without either one owning the other's data.

Grants are hierarchical: a grant on `acme/sales` also covers
`acme/sales/leads`. They are revocable instantly and can auto-expire.

### Also included

- **Automatic expiry.** Set `ttl_seconds` and stale facts remove themselves.
- **Deduplication.** Re-storing a near-identical fact updates it instead of
  creating a rival copy, so recall never has to choose between contradictions.
- **Cost transparency.** Every response includes `cost_usd`.
- **MCP server.** Works in Claude Desktop, Claude Code, Cursor and Windsurf
  with no integration code.
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
server-side credentials: never ship one in browser JavaScript or a mobile
bundle.

### Full documentation

https://boirama.github.io/agent-memory-api/
