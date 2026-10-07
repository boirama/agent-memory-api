"""Agent Memory API — Model Context Protocol server.

This is the piece that makes your API usable by agents rather than only by
developers. RapidAPI gets you humans who read listings; MCP gets you Claude
Desktop, Claude Code, Cursor and Windsurf calling your API as a native tool,
with no integration code on the user's side.

Install and run:

    pip install "mcp[cli]" httpx
    export AGENT_MEMORY_API_KEY="your-rapidapi-key"
    export AGENT_MEMORY_BASE_URL="https://agent-memory-api.yourname.workers.dev"
    python mcp/server.py

Then add to your MCP client config (Claude Desktop shown):

    {
      "mcpServers": {
        "agent-memory": {
          "command": "python",
          "args": ["/absolute/path/to/mcp/server.py"],
          "env": {
            "AGENT_MEMORY_API_KEY": "your-key",
            "AGENT_MEMORY_BASE_URL": "https://your-worker.workers.dev"
          }
        }
      }
    }

Tool descriptions here matter as much as the code. They are the only thing
the model sees when deciding whether to call a tool, so each one states WHEN
to use it, not just what it does.
"""

from __future__ import annotations

import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("AGENT_MEMORY_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("AGENT_MEMORY_API_KEY", "")
# Every memory this server writes is attributed to this agent id. Give each
# MCP client its own so permissions between them stay meaningful.
AGENT_ID = os.environ.get("AGENT_MEMORY_AGENT_ID", "mcp-client")
RAPIDAPI_HOST = os.environ.get("AGENT_MEMORY_RAPIDAPI_HOST", "")

mcp = FastMCP("agent-memory")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if RAPIDAPI_HOST:
        # Calling through the RapidAPI gateway.
        headers["X-RapidAPI-Key"] = API_KEY
        headers["X-RapidAPI-Host"] = RAPIDAPI_HOST
    elif API_KEY:
        # Calling your Worker or Railway URL directly.
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.request(
                method, f"{BASE_URL}{path}", headers=_headers(), **kwargs
            )
        except httpx.RequestError as exc:
            return {"error": f"Could not reach the memory API: {exc}"}

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:400]}
        return {"error": body.get("detail", body.get("error", "request failed")),
                "status": resp.status_code}
    return resp.json()


@mcp.tool()
async def remember(
    content: str,
    namespace: str = "default",
    importance: float | None = None,
    ttl_seconds: int | None = None,
    subject_id: str | None = None,
) -> str:
    """Save a fact so it survives past this conversation.

    Use this whenever the user tells you something worth knowing next time:
    a preference, a decision, a constraint, a name, a deadline. Prefer
    storing one clear fact per call over one long paragraph — recall ranks
    individual facts, not documents.

    Args:
        content: The fact, in a form that will still make sense with no
            surrounding conversation. Write "Sean prefers dark mode in the
            dashboard", not "he likes it dark".
        namespace: Slash-delimited grouping, e.g. "work/preferences".
            Permissions are granted per namespace, so group by who should
            eventually be able to read it.
        importance: 0.0-1.0. Omit unless you have a strong view; it is
            inferred from the content.
        ttl_seconds: Set this for facts with a shelf life — a current sprint,
            a temporary address. Omit for permanent facts.
        subject_id: Who the fact is about, if it is about a specific person.
    """
    payload: dict = {
        "content": content,
        "agent_id": AGENT_ID,
        "namespace": namespace,
    }
    if importance is not None:
        payload["importance"] = importance
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    if subject_id is not None:
        payload["subject_id"] = subject_id

    result = await _request("POST", "/v1/memories", json=payload)
    if "error" in result:
        return f"Could not save: {result['error']}"

    verb = "Updated existing memory" if result.get("status") == "updated" else "Saved"
    return f"{verb} ({result['memory_id']}) in {result['namespace']}."


@mcp.tool()
async def recall(
    query: str,
    namespace: str | None = None,
    limit: int = 5,
    include_shared: bool = True,
) -> str:
    """Look up what you already know before answering or asking.

    Call this at the START of a conversation, and again whenever the user
    refers to something you have no context for ("the usual", "like last
    time", "my preferences"). Asking the user something you could have
    recalled is the failure this tool exists to prevent.

    Args:
        query: A natural-language question, not keywords. "what does this
            user prefer for notifications" works better than "notifications".
        namespace: Restrict to a namespace and its children. Omit to search
            everything visible.
        limit: How many memories to return. 5 is usually plenty.
        include_shared: Include memories other agents have shared with you.
    """
    params: dict = {
        "query": query,
        "agent_id": AGENT_ID,
        "limit": limit,
        "include_shared": str(include_shared).lower(),
    }
    if namespace:
        params["namespace"] = namespace

    result = await _request("GET", "/v1/memories", params=params)
    if "error" in result:
        return f"Could not recall: {result['error']}"

    results = result.get("results", [])
    if not results:
        return "No relevant memories found."

    lines = []
    for i, m in enumerate(results, 1):
        origin = "" if m["via"] == "owned" else f" [shared by {m['agent_id']}]"
        lines.append(
            f"{i}. {m['content']}{origin}\n"
            f"   relevance {m['score']:.2f} | namespace {m['namespace']}"
        )
    return "\n".join(lines)


@mcp.tool()
async def forget(memory_id: str) -> str:
    """Delete a memory that is wrong or that the user asked you to forget.

    Use this the moment a user says something is incorrect or asks you to
    forget it. Do not merely store a correcting memory alongside the wrong
    one — both will surface in recall and the model cannot tell which wins.

    Args:
        memory_id: The UUID returned by remember(), or shown by recall().
    """
    result = await _request(
        "DELETE", f"/v1/memories/{memory_id}", params={"agent_id": AGENT_ID}
    )
    if "error" in result:
        return f"Could not forget: {result['error']}"
    if result.get("status") == "not_found":
        return "No such memory, or it belongs to a different agent."
    return f"Forgot {memory_id}."


@mcp.tool()
async def share_memory_with_agent(
    grantee_agent_id: str,
    namespace: str | None = None,
    memory_id: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Let another agent read your memories.

    Use this when the user wants a different assistant, bot or teammate's
    agent to have the context you hold. Grant a namespace for standing
    access; grant a single memory_id for a one-off hand-off.

    Args:
        grantee_agent_id: The agent receiving access. "*" means every agent
            on the account.
        namespace: Namespace to share. Covers child namespaces too — sharing
            "acme" also shares "acme/sales/leads".
        memory_id: Share exactly one memory instead of a namespace.
        ttl_seconds: Auto-revoke after this long. Prefer setting this.
    """
    if bool(namespace) == bool(memory_id):
        return "Provide exactly one of namespace or memory_id."

    payload: dict = {
        "granter_agent_id": AGENT_ID,
        "grantee_agent_id": grantee_agent_id,
        "permission": "read",
    }
    if namespace:
        payload["namespace"] = namespace
    if memory_id:
        payload["memory_id"] = memory_id
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds

    result = await _request("POST", "/v1/permissions", json=payload)
    if "error" in result:
        return f"Could not share: {result['error']}"
    target = namespace or memory_id
    return (
        f"Granted {grantee_agent_id} read access to {target}. "
        f"Revoke with permission id {result['permission_id']}."
    )


@mcp.tool()
async def memory_stats() -> str:
    """Report how much is stored and where.

    Use this when the user asks what you remember about them in general, or
    wants to audit which namespaces have grown.
    """
    result = await _request("GET", "/v1/stats")
    if "error" in result:
        return f"Could not read stats: {result['error']}"

    lines = [
        f"{result['active_memories']} active memories "
        f"({result['total_memories']} total, {result['expired_memories']} expired)",
        f"{result['total_agents']} registered agents",
        "",
        "By namespace:",
    ]
    for ns in result.get("namespaces", [])[:15]:
        lines.append(
            f"  {ns['namespace']}: {ns['count']} "
            f"(avg importance {ns['avg_importance']:.2f})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    if not API_KEY:
        print(
            "Warning: AGENT_MEMORY_API_KEY is unset. Calls will fail unless the "
            "API allows anonymous access.",
            file=sys.stderr,
        )
    mcp.run()
