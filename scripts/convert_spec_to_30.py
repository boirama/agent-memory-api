#!/usr/bin/env python3
"""Convert an OpenAPI 3.1 spec to 3.0.3 for consumers that can't read 3.1.

FastAPI emits 3.1, and some API marketplaces (RapidAPI among them) only
accept 3.0.x — the import fails silently with no useful error.

Three things differ in practice:

  1. Nullable fields. 3.1 writes {"anyOf": [{"type": "string"}, {"type": "null"}]};
     3.0 writes {"type": "string", "nullable": true}.
  2. Examples. 3.1 uses an "examples" ARRAY inside schemas; 3.0 uses a single
     "example". (3.0's "examples" is a map of Example Objects, not an array,
     so leaving it would be invalid.)
  3. "const": X became valid in 3.1; 3.0 expresses it as "enum": [X].
  4. Fields that only exist in 3.1: info.summary, info.license.identifier,
     jsonSchemaDialect, webhooks, components.pathItems. A 3.0 validator
     rejects the WHOLE document over any one of them, which is how the
     RapidAPI import fails with "should NOT have additional properties".

Everything else in a FastAPI spec is already 3.0-compatible.

Usage:
    python3 convert_spec_to_30.py site/openapi.json openapi-3.0.json
"""

from __future__ import annotations

import json
import sys


def convert(node):
    """Walk the spec, rewriting 3.1-only constructs in place."""
    if isinstance(node, list):
        return [convert(item) for item in node]
    if not isinstance(node, dict):
        return node

    node = {k: convert(v) for k, v in node.items()}

    # ---- 1. anyOf containing {"type": "null"} -> nullable ----------------
    if "anyOf" in node and isinstance(node["anyOf"], list):
        members = node["anyOf"]
        non_null = [m for m in members if m != {"type": "null"}]
        had_null = len(non_null) != len(members)

        if had_null:
            if len(non_null) == 1:
                # The common case: Optional[X]. Collapse to X + nullable,
                # keeping any sibling keys (title, description, default).
                siblings = {k: v for k, v in node.items() if k != "anyOf"}
                merged = dict(non_null[0])
                merged.update(siblings)
                merged["nullable"] = True
                node = merged
            else:
                # Optional[Union[...]] — keep the union, mark it nullable.
                node["anyOf"] = non_null
                node["nullable"] = True

    # ---- 2. examples array -> single example ----------------------------
    if isinstance(node.get("examples"), list) and node["examples"]:
        node["example"] = node["examples"][0]
        del node["examples"]

    # ---- 3. const -> single-value enum ----------------------------------
    if "const" in node:
        node["enum"] = [node.pop("const")]

    return node


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    src, dst = sys.argv[1], sys.argv[2]

    with open(src) as f:
        spec = json.load(f)

    original_version = spec.get("openapi", "unknown")
    if not original_version.startswith("3.1"):
        print(f"Input is OpenAPI {original_version} — no conversion needed.")
        return 0

    spec = convert(spec)
    spec["openapi"] = "3.0.3"

    # ---- fields that exist only in 3.1 ---------------------------------
    # A 3.0 validator rejects the entire document over any one of these,
    # so they must go even though they carry no schema meaning.
    spec.pop("webhooks", None)
    spec.pop("jsonSchemaDialect", None)
    spec.get("components", {}).pop("pathItems", None)

    info = spec.get("info", {})

    # info.summary is 3.1-only. Don't discard the text - fold it into the
    # description, which 3.0 does have, so the listing keeps the copy.
    summary = info.pop("summary", None)
    if summary:
        existing = info.get("description", "")
        info["description"] = f"{summary}\n\n{existing}".strip()

    # license.identifier is 3.1-only; 3.0 allows only name and url.
    info.get("license", {}).pop("identifier", None)

    with open(dst, "w") as f:
        json.dump(spec, f, indent=2)

    blob = json.dumps(spec)
    leftovers = {
        'anyOf with "type": "null"': blob.count('{"type": "null"}'),
        '"examples" arrays': blob.count('"examples": ['),
        '"const" keywords': blob.count('"const"'),
        "info.summary": 1 if "summary" in spec.get("info", {}) else 0,
        "jsonSchemaDialect": 1 if "jsonSchemaDialect" in spec else 0,
        "webhooks": 1 if "webhooks" in spec else 0,
    }

    print(f"{src} ({original_version}) -> {dst} (3.0.3)")
    print(f"  {len(spec.get('paths', {}))} paths, "
          f"{sum(len(v) for v in spec.get('paths', {}).values())} operations")

    bad = {k: n for k, n in leftovers.items() if n}
    if bad:
        print("  WARNING, 3.1-only constructs remain:")
        for k, n in bad.items():
            print(f"    {k}: {n}")
        return 1

    print("  no 3.1-only constructs remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
