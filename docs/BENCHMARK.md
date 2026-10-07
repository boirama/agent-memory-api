# Benchmark results

Run on the built-in `hashing` embedder against a local Postgres 16 with
pgvector 0.6.2. Reproduce with the script in the repo README.

## Recall accuracy

8 facts about a fictional customer, 7 natural-language probes:

| Query | Top-1 correct |
|---|---|
| how should we send invoices to Northwind | yes |
| when does the contract renew | yes |
| who handles billing | yes |
| what caused the outage | yes |
| what time do they like meetings | yes |
| what is the response time commitment for urgent issues | **no** |
| who should I avoid talking to about cost | yes |

**6/7 top-1 accuracy.**

## The miss is the point

The failed probe asked about "response time commitment for urgent issues".
The correct memory reads "The support SLA for Northwind is 4 hours for P1 and
24 hours for P2 tickets." They share no vocabulary — `SLA` and `response time
commitment` mean the same thing but look nothing alike.

This is exactly the limitation of feature hashing: it captures lexical and
morphological overlap, not meaning. Hybrid ranking's full-text component makes
it worse here, not better, because that component is also lexical.

Switching to `EMBEDDING_PROVIDER=openai` is what fixes this class of failure.
See Phase 10 in `DEPLOY_ADDENDUM.md`.

## Latency

- p50: **6.3 ms**
- max: **13.4 ms**

Local, single Postgres, no network. Add your Railway region round trip for
real figures. The Cloudflare Worker caches recalls at the edge for 20 seconds,
which absorbs repeat queries during a live conversation.

The ranking maths runs entirely in Postgres in one round trip. Pulling
candidates into Python to score them there costs more than the ranking saves.

## Test suite

24 integration tests against a real Postgres with pgvector, passing twice
consecutively (the suite truncates state in a fixture, so it is repeatable
rather than fresh-database-only).

Covered: store, dedupe, recall ranking, TTL expiry exclusion, single-memory
fetch with access control, soft and hard delete, non-owner delete rejection,
agent registration idempotency, isolation before grant, hierarchical namespace
grants, `include_shared=false`, permission listing, revocation, direct memory
shares, self-grant rejection, stats, billing rollups, flat validation errors,
`llms.txt` serving, and OpenAPI description coverage.
