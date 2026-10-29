# Launch copy (Phase 8)

## Product Hunt

**Tagline:** Persistent memory for AI agents, with permissions

**Description:**

Every AI agent forgets everything when the conversation ends. Agent Memory API
gives them somewhere to put what they learn — and a way to get it back that
isn't just vector similarity.

Recall scores three signals at once: semantic similarity, full-text match, and
salience. Salience decays for memories nobody touches and strengthens for ones
recalled often, so an agent's working set sharpens over time rather than
drowning in stale facts. Every result returns its individual sub-scores.

The part I haven't seen elsewhere: agents grant each other scoped access to
memory namespaces, like sharing a folder. A sales agent's notes on a lead
become readable by the support agent handling the ticket, without either one
owning the other's data. Grants are hierarchical, revocable, and can
auto-expire.

Ships with an MCP server, so it works in Claude Desktop, Claude Code, Cursor
and Windsurf with no integration code.

Free tier: 1,000 calls/month.

**First comment:**

Builder here. I kept hitting the same wall building multi-agent systems: each
agent had its own memory silo, and the moment two agents needed the same
customer context there was no clean way to share it without merging their
stores entirely.

The permission model is the part I'd most like feedback on. Right now grants
are namespace-level and hierarchical — a grant on `acme/sales` covers
`acme/sales/leads`. That felt like the right default, but I'm not sure whether
people will want per-memory grants as the primary path instead.

Also happy to be told the salience decay curve is wrong. It's a 46-day
half-life for untouched memories, which I picked by feel rather than by data.

---

## dev.to post

**Title:** Your AI Agent Forgets Everything. Here's the Architecture I Used to Fix It.

**Tags:** ai, python, postgres, api

**Body:**

Every agent framework has the same hole. The agent learns something useful,
the conversation ends, and it's gone. Next session it asks the user their
timezone again.

The obvious fix is a vector store. I tried that first, and it has a failure
mode nobody warns you about: **everything you ever stored competes forever**.
After a few thousand memories, recall starts surfacing facts that were true in
March. Vector similarity has no opinion about whether something still matters.

### Hybrid ranking

What I ended up with scores three signals in one Postgres query:

```sql
  0.55 * (1 - (embedding <=> query_vec))              -- semantic
+ 0.25 * LEAST(1.0, ts_rank_cd(search_vector, q) * 4) -- lexical
+ 0.20 * salience                                     -- importance over time
```

The lexical term matters more than I expected. Pure vector search is
surprisingly bad at exact identifiers — invoice numbers, person names, error
codes. Full-text search handles those precisely, and the two cover each
other's weaknesses.

### Salience is the interesting one

```sql
LEAST(1.0,
      importance * EXP(-decay_rate * days_since_access)
      + reinforcement * LN(1 + access_count))
```

Exponential decay on time since last access; logarithmic reinforcement on
recall count. A memory recalled forty times is load-bearing and stays strong.
A memory untouched for six months fades. Logarithmic rather than linear
reinforcement so hot memories saturate instead of running away with the
ranking.

All of it runs in Postgres in one round trip. My first version pulled
candidates into Python and scored them there, which meant the latency budget
went on the wire before any ranking happened.

### Permissions, not just storage

The thing that actually pushed me to build this rather than use an existing
library: multi-agent systems need *shared* memory with boundaries. A sales
agent and a support agent need overlapping context, but not the same store.

So grants are namespace-scoped and hierarchical. Granting `acme/sales` covers
`acme/sales/leads` and everything below. Revocable instantly, optionally
time-boxed. The visibility check is a single SQL predicate — owns it, has a
live namespace grant, or has a direct share — which keeps it in the same query
as the ranking rather than a second round trip.

### Things I got wrong first

**An unqueried index.** I added a trigram index for typo tolerance, then
noticed nothing queried it. It was taxing every INSERT for nothing. Deleted.

**Two unnecessary extension dependencies.** `pgcrypto` for
`gen_random_uuid()` — which has been in Postgres core since 13 — and
`pg_trgm` for that dead index. Both broke the migration on managed Postgres
builds that don't ship them.

**Prepared statements through a connection pooler.** Supabase's transaction
pooler multiplexes connections, which breaks asyncpg's prepared-statement
cache. Intermittent failures that only appear under real traffic.

**Tests that only passed once.** My suite asserted on absolute counts and
didn't reset state, so it passed on a fresh database and failed on the second
run. Truncating in a fixture was the honest fix — making the assertions
relative would have hidden the class of bug the assertions existed to catch.

### Stack

FastAPI, Postgres with pgvector, Redis for hot recalls, Cloudflare Workers at
the edge. Free tiers throughout until there's real traffic.

Docs: https://boirama.github.io/agent-memory-api/

---

## Where to post

- r/LocalLLaMA and r/MachineLearning — lead with the architecture, not the
  product. Pure launch posts get removed.
- Hacker News "Show HN" — the salience decay is the interesting angle
- LinkedIn and X — the "vector search has no opinion about whether something
  still matters" framing is the hook
- GitHub `awesome-ai-agents` and `awesome-mcp-servers` — submit a PR

**Post the dev.to article before the Product Hunt launch.** AI crawlers need
days to weeks to index new content, and the whole point of the
discoverability work is being the thing that gets cited when someone asks an
AI how to give their agent memory.
