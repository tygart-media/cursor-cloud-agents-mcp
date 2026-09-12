"""Transports for the Cursor Cloud Agents API (v1).

Two transports share one interface:

- :class:`RestCursorClient` — talks directly to https://api.cursor.com with a
  user API key from ``CURSOR_API_KEY``. Works anywhere. This is the default.
- :class:`SandboxCursorClient` — shells out to a ``cursor-agent`` CLI on PATH
  (the pattern used inside sandboxed agent environments that broker the
  credential for you). Select with ``CURSOR_TRANSPORT=sandbox`` or
  ``CURSOR_SANDBOX=1``. Never auto-detected: a stray ``cursor-agent`` binary
  on PATH must not hijack the transport.

Both implement :class:`CursorClient`, so the MCP tool layer never touches
HTTP or subprocesses directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_BASE = "https://api.cursor.com"
LAUNCH_TIMEOUT = int(os.environ.get("CURSOR_LAUNCH_TIMEOUT", "300"))
READ_TIMEOUT = 30
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class CursorAPIError(RuntimeError):
    """A failed Cursor API call, with the secret scrubbed out."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class CursorTimeout(CursorAPIError):
    """The request genuinely timed out. The server may still have acted —
    treat as UNKNOWN, never as failed."""


def _scrub(text: str, secret: str | None) -> str:
    if secret and secret in text:
        text = text.replace(secret, "[REDACTED]")
    return text


class CursorClient:
    """Interface both transports implement."""

    def launch(self, prompt: str, **kwargs) -> dict:
        raise NotImplementedError

    def get_agent(self, agent_id: str) -> dict:
        raise NotImplementedError

    def list_agents(self, limit: int = 20) -> dict:
        raise NotImplementedError

    def get_run(self, agent_id: str, run_id: str) -> dict:
        raise NotImplementedError

    def followup(self, agent_id: str, prompt: str) -> dict:
        raise NotImplementedError

    def cancel_run(self, agent_id: str, run_id: str) -> dict:
        raise NotImplementedError

    def list_models(self) -> dict:
        raise NotImplementedError

    def me(self) -> dict:
        raise NotImplementedError

    def usage(self, agent_id: str, run_id: str | None = None) -> dict:
        raise NotImplementedError


def _normalize_git(data: dict) -> dict:
    """API returns repoUrl without scheme; normalize to full https URLs."""
    git = data.get("git")
    if isinstance(git, dict):
        for branch in git.get("branches", []):
            url = branch.get("repoUrl", "")
            if url and "://" not in url:
                branch["repoUrl"] = "https://" + url
    return data


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, TimeoutError)
    return False


class RestCursorClient(CursorClient):
    """Direct REST transport. Key comes from ``CURSOR_API_KEY``."""

    def __init__(self, api_key: str | None = None,
                 base_url: str = API_BASE):
        self.api_key = api_key or os.environ.get("CURSOR_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = READ_TIMEOUT) -> dict:
        if not self.api_key:
            raise CursorAPIError(
                "CURSOR_API_KEY is not set. Mint a User API key at Cursor "
                "Dashboard → API Keys and export CURSOR_API_KEY=<key> "
                "(or set it in your MCP client's env config).")
        url = self.base_url + path
        data = None
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", "replace")
                err_json = json.loads(err_body)
                code = err_json.get("code")
                message = err_json.get("message", err_body)
            except Exception:
                code, message = None, f"HTTP {exc.code}"
            raise CursorAPIError(
                _scrub(f"Cursor API {exc.code}: {message}", self.api_key),
                status=exc.code, code=code)
        except Exception as exc:  # noqa: BLE001 - transport boundary
            msg = _scrub(str(exc), self.api_key)
            if _is_timeout(exc):
                raise CursorTimeout(
                    f"Cursor API request timed out after {timeout}s: {msg}")
            raise CursorAPIError(f"Network error calling Cursor API: {msg}")
        try:
            return _normalize_git(json.loads(body)) if body.strip() else {}
        except json.JSONDecodeError:
            raise CursorAPIError("Cursor API returned non-JSON response")

    # -- agents & runs ----------------------------------------------------

    def launch(self, prompt: str, **kwargs) -> dict:
        # Idempotent launch: mint the agent id client-side BEFORE the call,
        # so a retry after a read-timeout can never create a duplicate.
        idem = kwargs.get("idempotency_key")
        if idem:
            agent_id = f"bc-{uuid.uuid5(uuid.NAMESPACE_URL, str(idem))}"
        else:
            agent_id = f"bc-{uuid.uuid4()}"

        model = kwargs.get("model")
        # The literal string "Auto" is a 400; omit to use the default chain.
        if isinstance(model, str) and model.strip().lower() == "auto":
            model = None

        payload: dict = {
            "agentId": agent_id,
            "prompt": {"text": prompt},
        }
        if kwargs.get("name"):
            payload["name"] = kwargs["name"][:100]
        if model:
            payload["model"] = {"id": model}
            if kwargs.get("model_params"):
                payload["model"]["params"] = kwargs["model_params"]
        if kwargs.get("repo_url"):
            payload["repos"] = [{"url": kwargs["repo_url"]}]
            # Never default startingRef to main; only send what was given.
            if kwargs.get("starting_ref"):
                payload["repos"][0]["startingRef"] = kwargs["starting_ref"]
        if kwargs.get("mode") in ("agent", "plan"):
            payload["mode"] = kwargs["mode"]

        try:
            return self._request("POST", "/v1/agents", payload,
                                 timeout=LAUNCH_TIMEOUT)
        except CursorTimeout:
            # We minted agent_id client-side: check whether it landed.
            try:
                agent = self.get_agent(agent_id)
            except CursorAPIError as exc2:
                if exc2.status == 404:
                    raise CursorAPIError(
                        "Launch timed out and the agent was not created. "
                        "Retry the launch with the same idempotency_key — "
                        "replays are safe and never duplicate.")
                return {"status": "unknown", "agent_id_hint": agent_id,
                        "reconcile_hint": "call cursor_list and match by "
                                          "creation time"}
            run_id = agent.get("latestRunId") or ""
            run = self.get_run(agent_id, run_id) if run_id else {}
            return {"agent": agent, "run": run,
                    "reconciled_after_timeout": True}
        except CursorAPIError as exc:
            if exc.status == 409 and exc.code == "agent_id_conflict":
                # Our own retry landed: return the SAME shape as success.
                agent = self.get_agent(agent_id)
                run_id = agent.get("latestRunId") or ""
                run = self.get_run(agent_id, run_id) if run_id else {}
                return {"agent": agent, "run": run, "replayed": True}
            raise

    def get_agent(self, agent_id: str) -> dict:
        return self._request("GET", f"/v1/agents/{agent_id}")

    def list_agents(self, limit: int = 20) -> dict:
        q = urllib.parse.urlencode({"limit": max(1, min(limit, 100))})
        return self._request("GET", f"/v1/agents?{q}")

    def get_run(self, agent_id: str, run_id: str) -> dict:
        return self._request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")

    def followup(self, agent_id: str, prompt: str) -> dict:
        try:
            return self._request("POST", f"/v1/agents/{agent_id}/runs",
                                 {"prompt": {"text": prompt}},
                                 timeout=LAUNCH_TIMEOUT)
        except CursorAPIError as exc:
            if exc.status == 409:
                raise CursorAPIError(
                    "Agent is busy (a run is still active). Poll cursor_status "
                    "until the run is terminal, then send the follow-up.")
            raise

    def cancel_run(self, agent_id: str, run_id: str) -> dict:
        try:
            self._request(
                "POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel", {})
        except CursorAPIError as exc:
            if exc.status == 409:
                raise CursorAPIError(
                    "Run is already terminal (run_not_cancellable).")
            raise
        # Report the run's actual post-cancel state, not an assumption.
        return self.get_run(agent_id, run_id)

    def list_models(self) -> dict:
        return self._request("GET", "/v1/models")

    def me(self) -> dict:
        return self._request("GET", "/v1/me")

    def usage(self, agent_id: str, run_id: str | None = None) -> dict:
        path = f"/v1/agents/{agent_id}/usage"
        if run_id:
            path += "?" + urllib.parse.urlencode({"runId": run_id})
        return self._request("GET", path)


class SandboxCursorClient(CursorClient):
    """Subprocess transport for sandboxed environments.

    Expects a ``cursor-agent`` executable on PATH implementing this JSON CLI
    contract (each command prints a JSON object to stdout):

    - ``me``, ``models``
    - ``launch`` — prompt on STDIN; flags: ``--name``, ``--model``,
      ``--model-params-json``, ``--repo``, ``--ref``, ``--mode``,
      ``--agent-id`` (client-minted ``bc-<uuid>`` for idempotent replay)
    - ``get --agent <id>``, ``list --limit <n>``
    - ``run --agent <id> --run <id>``
    - ``followup --agent <id>`` — prompt on STDIN
    - ``cancel --agent <id> --run <id>``
    - ``usage --agent <id> [--run <id>]``

    Values that could be mistaken for flags go after a ``--`` separator.
    The sandbox broker supplies the credential; this transport never sees
    a key.
    """

    def __init__(self, cli: str = "cursor-agent", timeout: int = READ_TIMEOUT):
        self.cli = cli
        self.timeout = timeout

    def _run(self, *args: str, prompt: str | None = None,
             timeout: int | None = None) -> dict:
        try:
            proc = subprocess.run(
                [self.cli, *args], input=prompt, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout or self.timeout)
        except FileNotFoundError:
            raise CursorAPIError(
                f"'{self.cli}' not found on PATH. The sandbox transport needs "
                "a cursor-agent CLI; use CURSOR_TRANSPORT=rest with "
                "CURSOR_API_KEY instead.")
        except subprocess.TimeoutExpired:
            raise CursorTimeout(
                f"'{self.cli}' timed out; the Cursor-side operation may still "
                "have succeeded — reconcile with cursor_list.")
        except OSError as exc:
            raise CursorAPIError(f"Failed to run '{self.cli}': {exc}")
        if proc.returncode != 0:
            raise CursorAPIError(
                (proc.stderr or proc.stdout or "unknown CLI error")[:500])
        try:
            return _normalize_git(json.loads(proc.stdout))
        except json.JSONDecodeError:
            raise CursorAPIError("cursor-agent CLI returned non-JSON output")

    def launch(self, prompt: str, **kwargs) -> dict:
        idem = kwargs.get("idempotency_key")
        agent_id = (f"bc-{uuid.uuid5(uuid.NAMESPACE_URL, str(idem))}"
                    if idem else f"bc-{uuid.uuid4()}")
        model = kwargs.get("model")
        if isinstance(model, str) and model.strip().lower() == "auto":
            model = None
        args = ["launch", "--agent-id", agent_id]
        if kwargs.get("name"):
            args += ["--name", kwargs["name"]]
        if model:
            args += ["--model", model]
        if kwargs.get("model_params"):
            args += ["--model-params-json", json.dumps(kwargs["model_params"])]
        if kwargs.get("repo_url"):
            args += ["--repo", kwargs["repo_url"]]
        if kwargs.get("starting_ref"):
            args += ["--ref", kwargs["starting_ref"]]
        if kwargs.get("mode") in ("agent", "plan"):
            args += ["--mode", kwargs["mode"]]
        args.append("--")  # values below are never parsed as flags
        return self._run(*args, prompt=prompt, timeout=LAUNCH_TIMEOUT)

    def get_agent(self, agent_id: str) -> dict:
        return self._run("get", "--agent", "--", agent_id)

    def list_agents(self, limit: int = 20) -> dict:
        return self._run("list", "--limit", str(limit))

    def get_run(self, agent_id: str, run_id: str) -> dict:
        return self._run("run", "--agent", "--", agent_id, "--run", run_id)

    def followup(self, agent_id: str, prompt: str) -> dict:
        return self._run("followup", "--agent", "--", agent_id,
                         prompt=prompt, timeout=LAUNCH_TIMEOUT)

    def cancel_run(self, agent_id: str, run_id: str) -> dict:
        return self._run("cancel", "--agent", "--", agent_id,
                         "--run", run_id)

    def list_models(self) -> dict:
        return self._run("models")

    def me(self) -> dict:
        return self._run("me")

    def usage(self, agent_id: str, run_id: str | None = None) -> dict:
        args = ["usage", "--agent", "--", agent_id]
        if run_id:
            args += ["--run", run_id]
        return self._run(*args)


def default_client() -> CursorClient:
    """Pick transport. Explicit only — never sniff PATH for a CLI.

    ``CURSOR_TRANSPORT=sandbox`` (or ``CURSOR_SANDBOX=1``) selects the
    subprocess transport; anything else, including unset, is REST.
    """
    transport = os.environ.get("CURSOR_TRANSPORT", "").strip().lower()
    if transport in ("sandbox", "cli") or \
            os.environ.get("CURSOR_SANDBOX") == "1":
        return SandboxCursorClient()
    if transport in ("rest", "direct", ""):
        return RestCursorClient()
    raise CursorAPIError(f"Unknown CURSOR_TRANSPORT={transport!r} "
                         "(use 'rest' or 'sandbox')")
