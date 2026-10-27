/**
 * Agent Memory API — Cloudflare Worker edge proxy
 *
 * PHASE 5, STEP 3: change ORIGIN below to your Railway URL, then run
 * `wrangler deploy`. That is the only edit this file needs.
 *
 * What it buys you:
 *   - Global edge termination, so a call from Singapore doesn't pay the full
 *     round trip to your single Railway region.
 *   - Edge caching of recall responses, which are the hot path.
 *   - A stable public URL independent of your backend. RapidAPI points at
 *     this Worker, not at Railway, so you can migrate off Railway later
 *     without re-listing your API.
 */

// ---------------------------------------------------------------------------
// EDIT THIS LINE  ⬇
const ORIGIN = "https://agent-memory-api-production-394c.up.railway.app";
// ---------------------------------------------------------------------------

const CACHE_TTL_SECONDS = 20;

// Only GETs are cacheable, and only these paths. Never cache writes, and
// never cache billing — a stale usage figure is worse than a slow one.
const CACHEABLE_PATHS = ["/v1/memories", "/v1/stats"];

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers":
    "Content-Type, Authorization, X-RapidAPI-Key, X-RapidAPI-Host, " +
    "X-RapidAPI-User, X-RapidAPI-Subscription, X-RapidAPI-Proxy-Secret, X-Request-ID",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const origin = env.ORIGIN_URL || ORIGIN;
    const upstream = new URL(url.pathname + url.search, origin);

    const isCacheable =
      request.method === "GET" &&
      CACHEABLE_PATHS.some((p) => url.pathname.startsWith(p));

    // The cache key MUST include caller identity, or one tenant's memories
    // get served to another. This is the most dangerous line in this file —
    // do not "simplify" it.
    let cacheKey = null;
    if (isCacheable) {
      const identity =
        request.headers.get("X-RapidAPI-User") ||
        request.headers.get("Authorization") ||
        "anon";
      const keyUrl = new URL(upstream);
      keyUrl.searchParams.set("__id", await sha256(identity));
      cacheKey = new Request(keyUrl.toString(), { method: "GET" });

      const hit = await caches.default.match(cacheKey);
      if (hit) {
        const res = new Response(hit.body, hit);
        res.headers.set("X-Edge-Cache", "HIT");
        applyCors(res);
        return res;
      }
    }

    const headers = new Headers(request.headers);
    headers.set("Host", upstream.host);
    headers.set("X-Forwarded-Proto", "https");
    headers.set("X-Edge-Region", request.cf?.colo || "unknown");
    if (request.cf?.country) headers.set("X-Client-Country", request.cf.country);

    let upstreamResponse;
    try {
      upstreamResponse = await fetch(upstream.toString(), {
        method: request.method,
        headers,
        body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
        redirect: "follow",
      });
    } catch (err) {
      // Railway free-tier containers sleep. A cold start looks like a
      // connection failure from here, so say so rather than returning a bare
      // 500 that tells the caller nothing.
      return json(
        {
          error: "origin_unreachable",
          detail:
            "The API origin did not respond. If this is a free-tier deployment " +
            "it may be cold-starting; retry in a few seconds.",
        },
        502
      );
    }

    const response = new Response(upstreamResponse.body, upstreamResponse);
    applyCors(response);
    response.headers.set("X-Edge-Cache", isCacheable ? "MISS" : "BYPASS");

    if (isCacheable && upstreamResponse.status === 200) {
      const toCache = response.clone();
      toCache.headers.set("Cache-Control", `public, max-age=${CACHE_TTL_SECONDS}`);
      ctx.waitUntil(caches.default.put(cacheKey, toCache));
    }

    return response;
  },
};

function applyCors(response) {
  for (const [k, v] of Object.entries(CORS_HEADERS)) {
    response.headers.set(k, v);
  }
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

async function sha256(value) {
  const data = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .slice(0, 8)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
