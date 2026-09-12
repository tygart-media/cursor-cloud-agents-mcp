"""MCP server: drive Cursor Cloud Agents from any MCP client.

Tools are thin wrappers over :mod:`cursor_cloud_agents_mcp.client`; every piece of
hard-won API knowledge lives in the tool descriptions so the calling model
gets it right without reading docs.
"""

from __future__ import annotations

import anyio
import anyio.to_thread

from mcp.server.fastmcp import FastMCP

from .client import (TRANSIENT_STATUSES, CursorAPIError, CursorClient,
                     default_client)

mcp = FastMCP("cursor-cloud-agents")

_client: CursorClient | None = None


def _client_or_error() -> CursorClient:
    global _client
    if _client is None:
        _client = default_client()  # raises CursorAPIError on bad config
    return _client


TERMINAL = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED"}


def _run_state(agent_id: str, run_id: str) -> dict:
    run = _client_or_error().get_run(agent_id, run_id)
    return {
        "agent_id": agent_id,
        "run_id": run.get("id", run_id),
        "status": run.get("status"),
        "terminal": run.get("status") in TERMINAL,
        "durationMs": run.get("durationMs"),
        "result": run.get("result") if run.get("status") in TERMINAL else None,
        "git": run.get("git"),
    }


@mcp.tool()
def cursor_launch(prompt: str, repo_url: str = "", starting_ref: str = "",
                  model: str = "", name: str = "", mode: str = "",
                  model_params: list = [],  # noqa: B006 - FastMCP copies schema
                  idempotency_key: str = "") -> dict:
    """Launch a Cursor cloud agent and enqueue its first run.

    Pass a complete task prompt: goal, constraints, and how to verify done —
    vague prompts produce vague agents. Repo-less launches (no repo_url) are
    for research, reviews, and writing; pass repo_url (a GitHub https URL) when
    the agent should write code. starting_ref is a branch or SHA; omit it to
    use the repo default (never assume "main").

    Call cursor_models first and pass a model id verbatim — never guess ids,
    and never pass the literal string "Auto" (omit model instead). model_params
    is a list of {key, value} dicts for per-model options (only ids/params
    from cursor_models are accepted). mode is "agent" or "plan".

    Returns agent_id AND run_id: poll cursor_status with both. Launch can take
    minutes; if the tool reports status "unknown", the agent may still have
    been created — reconcile with cursor_list, or retry with the same
    idempotency_key (replays are safe and never duplicate).
    """
    try:
        res = _client_or_error().launch(
            prompt,
            repo_url=repo_url or None,
            starting_ref=starting_ref or None,
            model=model or None,
            name=name or None,
            mode=mode or None,
            model_params=model_params or None,
            idempotency_key=idempotency_key or None,
        )
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}
    if res.get("status") == "unknown":
        return {"ok": False, **res}
    agent = res.get("agent", {})
    run = res.get("run", {})
    out = {"ok": True,
           "agent_id": agent.get("id"),
           "run_id": run.get("id"),
           "url": agent.get("url"),
           "agent_status": agent.get("status"),
           "run_status": run.get("status")}
    if res.get("replayed"):
        out["replayed"] = True
    if res.get("reconciled_after_timeout"):
        out["reconciled_after_timeout"] = True
    return out


@mcp.tool()
def cursor_status(agent_id: str, run_id: str = "") -> dict:
    """Check a run's status. Run status is the source of truth — the agent's
    lifecycle status (ACTIVE/IDLE) is NOT "is it still working".

    If run_id is omitted, the agent's latest run is used. Poll this every
    10-30 seconds; never sleep inside a tool call. When terminal=true, result
    holds the agent's final reply.
    """
    try:
        if not run_id:
            agent = _client_or_error().get_agent(agent_id)
            run_id = agent.get("latestRunId", "")
            if not run_id:
                return {"ok": False,
                        "error": "agent has no runs yet; retry shortly"}
        return {"ok": True, **_run_state(agent_id, run_id)}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def cursor_result(agent_id: str, run_id: str = "",
                        timeout_s: int = 120) -> dict:
    """Wait for a run to finish (polling server-side) and return its result.

    Convenience wrapper over cursor_status for when you just want the final
    answer. Waits at most timeout_s seconds (clamped to 5 min) — on timeout,
    keep polling with cursor_status using the returned run_id. Large results
    are truncated with truncated=true. Transient API blips (429/5xx) are
    retried with backoff.
    """
    timeout_s = max(10, min(timeout_s, 300))
    deadline = anyio.current_time() + timeout_s
    failures = 0
    try:
        client = _client_or_error()
        if not run_id:
            agent = await anyio.to_thread.run_sync(client.get_agent, agent_id)
            run_id = agent.get("latestRunId", "")
            if not run_id:
                return {"ok": False,
                        "error": "agent has no runs yet; retry shortly",
                        "agent_id": agent_id}
        while anyio.current_time() < deadline:
            try:
                state = await anyio.to_thread.run_sync(
                    _run_state, agent_id, run_id)
                failures = 0
            except CursorAPIError as exc:
                if exc.status in TRANSIENT_STATUSES or exc.status is None:
                    failures += 1
                    if failures > 3:
                        raise
                    await anyio.sleep(min(2 ** failures, 30))
                    continue
                raise
            if state.get("status") is None:
                return {"ok": False, "error": "unexpected run shape",
                        "agent_id": agent_id, "run_id": run_id}
            if state["terminal"]:
                result = state.get("result") or ""
                if len(result) > 6000:
                    result = result[:6000]
                    state["truncated"] = True
                state["result"] = result
                return {"ok": True, **state}
            await anyio.sleep(15)
        return {"ok": False, "error": "timed out waiting",
                "agent_id": agent_id, "run_id": run_id,
                "hint": "keep polling with cursor_status"}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def cursor_followup(agent_id: str, prompt: str) -> dict:
    """Send a follow-up prompt to an agent. Starts a NEW run — the returned
    run_id is what you poll with cursor_status (the old run_id is stale).

    Only call when the previous run is terminal; a follow-up during an active
    run fails with "agent is busy" — poll cursor_status first.
    """
    try:
        res = _client_or_error().followup(agent_id, prompt)
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}
    run = res.get("run", res)
    return {"ok": True, "agent_id": agent_id,
            "run_id": run.get("id"), "run_status": run.get("status")}


@mcp.tool()
def cursor_cancel(agent_id: str, run_id: str) -> dict:
    """Cancel the active run. Cancellation is terminal — to continue the
    conversation, send a follow-up (which starts a new run on the same agent).
    Does not archive the agent. Returns the run's actual post-cancel state.
    """
    try:
        run = _client_or_error().cancel_run(agent_id, run_id)
        return {"ok": True, "agent_id": agent_id,
                "run_id": run.get("id", run_id),
                "status": run.get("status")}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def cursor_list(limit: int = 10) -> dict:
    """List your agents, newest first. Use this to reconcile after a launch
    reported status "unknown" (match by name/creation time), or to find an
    agent you lost track of.
    """
    try:
        res = _client_or_error().list_agents(limit)
        items = res.get("items", res.get("agents", []))
        return {"ok": True, "agents": [
            {"agent_id": a.get("id"), "name": a.get("name"),
             "status": a.get("status"), "createdAt": a.get("createdAt"),
             "latestRunId": a.get("latestRunId"),
             "url": a.get("url")} for a in items]}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def cursor_models() -> dict:
    """List available Cursor models with their ids, parameters, and variants.
    Call this before cursor_launch and pass the id verbatim — ids are not
    guessable. Each model may support params (e.g. reasoning effort); only
    valid id/params combinations from this list will be accepted.
    """
    try:
        res = _client_or_error().list_models()
        models = res.get("models", res.get("items", []))
        return {"ok": True, "models": models}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def cursor_whoami() -> dict:
    """Verify your API key works and see which identity it belongs to.
    Call this first if anything else fails with auth errors.
    """
    try:
        return {"ok": True, **_client_or_error().me()}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def cursor_usage(agent_id: str, run_id: str = "") -> dict:
    """Token usage and cost for an agent (or one run). Costs real money —
    check this when a run felt expensive.
    """
    try:
        res = _client_or_error().usage(agent_id, run_id or None)
        return {"ok": True, **res}
    except CursorAPIError as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    mcp.run()
