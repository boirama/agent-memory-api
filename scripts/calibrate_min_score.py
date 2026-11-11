#!/usr/bin/env python3
"""Calibrate the recommended min_score against the LIVE API (via RapidAPI).

Stores a small set of test memories with deliberately varied importance,
asks paraphrased questions (few shared words), and reports for each one:

  * the score of the memory that SHOULD answer it
  * the highest score of any memory that should NOT

A good min_score sits above every "wrong" score and below every "right" one.
The script suggests a value, or tells you if no single value works.

Standard library only - no venv needed. Your RapidAPI key is typed into a
hidden prompt and never written anywhere.

Usage (from the repo root):
    python3 scripts/calibrate_min_score.py          # run, then delete test memories
    python3 scripts/calibrate_min_score.py --keep   # run, keep them for inspection

Costs about 17 requests from your plan's monthly quota (more if retries happen).
"""

from __future__ import annotations

import getpass
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "agent-memory-api1.p.rapidapi.com"
BASE = f"https://{HOST}"
AGENT = "launch-test"
NAMESPACE = "launch/calib"
PAUSE = 3.0  # seconds between calls
RETRIES = 5  # attempts per call when the gateway returns a spurious 401 / 429

# key -> (content, importance). Importance varies on purpose: a LOW-importance
# right answer and a HIGH-importance distractor are the hard cases for a floor
# on the combined score, because importance alone contributes up to ~0.2.
MEMORIES = {
    "sla":     ("The support SLA is 4 hours for P1 and 24 hours for P2 tickets", 0.8),
    "thabo":   ("Customer Thabo prefers email over SMS for notifications", 0.5),
    "invoice": ("Invoices are issued on the 25th of each month", 0.3),
    "staging": ("Our staging database is restored from production nightly", 0.5),
    "wifi":    ("The office Wi-Fi password rotates every Monday", 1.0),
}

# (question, key of the memory that should answer it, or None = nothing should)
QUERIES = [
    ("what is the response time commitment for urgent issues", "sla"),
    ("how should I get in touch with that client", "thabo"),
    ("when do bills go out", "invoice"),
    ("how fresh is the data in the test environment", "staging"),
    ("favourite pizza toppings", None),
    ("who won the football match last night", None),
]


def call(method: str, path: str, key: str, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-RapidAPI-Key": key,
        "X-RapidAPI-Host": HOST,
        "Content-Type": "application/json",
        "User-Agent": "agent-memory-calibrate/1.0",
    })
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                time.sleep(PAUSE)
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # The RapidAPI gateway intermittently answers a VALID key with
            # 401 "Invalid API key" (the request never reaches the API), and
            # 429 means slow down. Both are worth retrying; anything else is not.
            gateway_flake = e.code == 401 and "Invalid API key" in detail
            if (gateway_flake or e.code == 429) and attempt < RETRIES:
                print(f"    (HTTP {e.code} from gateway, retry {attempt}/{RETRIES - 1})")
                time.sleep(5 * attempt)
                continue
            sys.exit(f"\n{method} {path} failed: HTTP {e.code}\n{detail}\n")
        except urllib.error.URLError as e:
            sys.exit(f"\nCould not reach {HOST}: {e.reason}\n")


def floor05(x: float) -> float:
    return math.floor(x * 20) / 20


# Candidate (semantic, lexical, salience) weights. All re-score the SAME
# recall results offline - no extra API calls. Current production first.
WEIGHT_SETS = [
    (0.55, 0.25, 0.20),
    (0.70, 0.20, 0.10),
    (0.75, 0.15, 0.10),
    (0.80, 0.15, 0.05),
    (0.85, 0.15, 0.00),
]


def simulate(observed) -> None:
    print("\nPer-memory signals (semantic / lexical / salience):")
    for question, expect, comps in observed:
        print(f"  {question[:46]}")
        for name, (sem, lex, sal) in sorted(comps.items(), key=lambda kv: -kv[1][0]):
            mark = "  <- right" if name == expect else ""
            print(f"      {name:<8} {sem:.3f} / {lex:.3f} / {sal:.3f}{mark}")

    print("\nWhat-if: the same results re-scored with other weights")
    print(f"{'sem/lex/sal':<16}{'misranked':>10}{'lowest right':>14}"
          f"{'highest wrong':>15}  verdict")
    print("-" * 90)
    for ws, wl, wsal in WEIGHT_SETS:
        rights, wrongs, bad = [], [], 0
        for _, expect, comps in observed:
            scored = {n: ws * a + wl * b + wsal * c for n, (a, b, c) in comps.items()}
            top = max(scored, key=scored.get)
            if expect is not None:
                rights.append(scored[expect])
                bad += top != expect
            wrongs.append(max((v for n, v in scored.items() if n != expect), default=0.0))
        r_min, w_max = min(rights), max(wrongs)
        if bad == 0 and r_min > w_max:
            sug = floor05(w_max + 0.4 * (r_min - w_max))
            if sug <= w_max:
                sug = round(w_max + 0.01, 2)
            verdict = f"OK, min_score ~ {sug:.2f}"
        elif bad == 0:
            verdict = "ranks right, floor overlaps"
        else:
            verdict = "wrong memory ranked first"
        label = f"{ws:.2f}/{wl:.2f}/{wsal:.2f}" + (" *" if (ws, wl, wsal) == WEIGHT_SETS[0] else "")
        print(f"{label:<16}{bad:>10}{r_min:>14.3f}{w_max:>15.3f}  {verdict}")
    print("(* = current production weights)")


def main() -> int:
    keep = "--keep" in sys.argv
    key = getpass.getpass("RapidAPI key (hidden): ").strip()
    if not key:
        sys.exit("No key entered.")

    # ---- 1. store --------------------------------------------------------
    print(f"\nStoring {len(MEMORIES)} test memories in {NAMESPACE} ...")
    ids: dict[str, str] = {}
    for name, (content, imp) in MEMORIES.items():
        status, r = call("POST", "/v1/memories", key, body={
            "content": content, "agent_id": AGENT,
            "namespace": NAMESPACE, "importance": imp,
        })
        ids[name] = r["memory_id"]
        print(f"  {status}  {name:<8} importance={imp}  ({r.get('status', '')})")
    by_id = {v: k for k, v in ids.items()}

    # ---- 2. recall -------------------------------------------------------
    print("\nRecalling (min_score=0, so every memory comes back scored) ...\n")
    print(f"{'question':<50} {'right':>7} {'worst wrong':>12}  top hit")
    print("-" * 90)

    rights: list[float] = []
    wrongs: list[float] = []
    misranked = []
    observed = []  # (question, expect, {name: (semantic, lexical, salience)})
    for question, expect in QUERIES:
        _, r = call("GET", "/v1/memories", key, params={
            "query": question, "agent_id": AGENT, "namespace": NAMESPACE,
            "limit": 10, "min_score": 0, "min_importance": 0,
        })
        results = [x for x in r.get("results", []) if x["memory_id"] in by_id]
        if not results:
            sys.exit(f"\nNo results for '{question}' - check the namespace and filters.")

        right = None
        worst_wrong = 0.0
        for x in results:
            name = by_id[x["memory_id"]]
            if name == expect:
                right = x["score"]
            else:
                worst_wrong = max(worst_wrong, x["score"])
        top = by_id[results[0]["memory_id"]]
        observed.append((question, expect, {
            by_id[x["memory_id"]]: (max(0.0, x.get("semantic_score", 0.0)),
                                     x.get("lexical_score", 0.0),
                                     x.get("salience_score", 0.0))
            for x in results
        }))

        if expect is not None:
            if right is None:
                sys.exit(f"\nExpected memory '{expect}' missing for '{question}'.")
            rights.append(right)
            if top != expect:
                misranked.append((question, expect, top))
        wrongs.append(worst_wrong)

        right_txt = f"{right:.3f}" if right is not None else "  -  "
        print(f"{question[:50]:<50} {right_txt:>7} {worst_wrong:>12.3f}  {top}")

    # ---- 3. verdict ------------------------------------------------------
    r_min, w_max = min(rights), max(wrongs)
    print("-" * 90)
    print(f"Lowest RIGHT score : {r_min:.3f}")
    print(f"Highest WRONG score: {w_max:.3f}")

    if misranked:
        print("\nWARNING - the right memory was not ranked first for:")
        for q, exp, top in misranked:
            print(f"  '{q}': expected {exp}, got {top}")

    if r_min > w_max:
        # Lean towards the wrong side so real answers aren't dropped.
        suggestion = floor05(w_max + 0.4 * (r_min - w_max))
        if suggestion <= w_max:
            suggestion = round(w_max + 0.01, 2)
        print(f"\nClean separation. Suggested min_score: {suggestion:.2f}")
        print(f"(gap is {r_min - w_max:.3f}; anything between {w_max:.2f} "
              f"and {r_min:.2f} would work on this data)")
    else:
        print("\nOVERLAP - no single min_score separates right from wrong here.")
        print("A high-importance unrelated memory can outscore a low-importance")
        print("relevant one, because importance feeds the score being filtered.")
        print("Recommend the docs say 'start around 0.3 and tune for your data',")
        print("and see fix H10 in the launch guide.")

    simulate(observed)

    # ---- 4. clean up -----------------------------------------------------
    if keep:
        print(f"\n--keep: test memories left in {NAMESPACE}.")
    else:
        print("\nDeleting test memories ...")
        for name, mid in ids.items():
            status, _ = call("DELETE", f"/v1/memories/{mid}", key,
                             params={"agent_id": AGENT, "hard": "true"})
            print(f"  {status}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
