"""Export the OpenAPI spec to the docs site.

Run this after changing any endpoint, then commit site/openapi.json. The
spec is the strongest machine-readable discovery signal you have, and a
stale one is worse than none: an AI agent that generates a call against a
parameter you removed produces a broken snippet with your name on it.

    python scripts/export_openapi.py
"""
import json, os, pathlib, sys

# Run from the repo root as `python scripts/export_openapi.py`. sys.path[0]
# is this file's directory, so the project root has to be added explicitly.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/x")
from app.main import app

spec = app.openapi()
# Advertise the public URL, not localhost.
spec["servers"] = [
    {"url": "https://agent-memory-api.YOUR-NAME.workers.dev",
     "description": "Production (Cloudflare edge)"},
    {"url": "https://agent-memory-api.p.rapidapi.com",
     "description": "Via RapidAPI gateway"},
]
spec["externalDocs"] = {
    "description": "Guides and integration examples",
    "url": "https://YOUR-GITHUB-USERNAME.github.io/agent-memory-api/",
}

out = pathlib.Path("site/openapi.json")
out.write_text(json.dumps(spec, indent=2))

paths = sum(len(v) for v in spec["paths"].values())
missing = [
    f"{m.upper()} {p}"
    for p, ops in spec["paths"].items()
    for m, op in ops.items()
    if not op.get("description")
]
print(f"wrote {out} - {len(spec['paths'])} paths, {paths} operations")
if missing:
    print("operations lacking a description (AI agents match on these):")
    for m in missing:
        print("  -", m)
else:
    print("every operation has a summary and description")
