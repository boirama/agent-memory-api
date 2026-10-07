# Placeholders you must replace before publishing

Three find-and-replace operations across the repo. In VS Code: **Ctrl+Shift+H**.

| Find | Replace with | Example |
|---|---|---|
| `YOUR-GITHUB-USERNAME` | your GitHub username | `seandouglas` |
| `YOUR-NAME` | your Cloudflare Workers subdomain | `seandouglas` |
| `YOUR-USERNAME` | your RapidAPI username | `sdkh` |

Plus one manual edit:

| File | Line | Change |
|---|---|---|
| `worker.js` | `const ORIGIN = ...` | your Railway URL from Phase 4 |

## Files containing placeholders

```
docs/DEPLOY_ADDENDUM.md
docs/LAUNCH.md
docs/RAPIDAPI_LISTING.md
mcp/claude_desktop_config.example.json
scripts/export_openapi.py
site/index.html
site/llms.txt
site/openapi.json
site/robots.txt
site/sitemap.xml
site/guides/auto-expire-stale-memories.html
site/guides/mcp-server.html
site/guides/persist-agent-memory-across-sessions.html
site/guides/share-context-between-agents.html
worker.js
```

After replacing, regenerate the spec so it picks up the real URLs:

```bash
python scripts/export_openapi.py
```

## Then verify

- `https://YOUR-USERNAME.github.io/agent-memory-api/llms.txt` returns text
- `https://YOUR-USERNAME.github.io/agent-memory-api/openapi.json` returns JSON
- Paste the docs homepage into Google's Rich Results Test — the WebAPI block parses
- `https://YOUR-RAILWAY-URL/health` returns `{"status":"healthy",...}`
- `https://YOUR-RAILWAY-URL/health/schema` returns `"migrations_applied": true`
