"""Unit tests with mocked urllib — no network, no key needed."""

import io
import json
import sys
import urllib.error

sys.path.insert(0, "src")

from cursor_cloud_agents_mcp.client import (  # noqa: E402
    CursorAPIError, RestCursorClient, _normalize_git,
)


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_client(monkeypatch, handler):
    monkeypatch.setattr("urllib.request.urlopen", handler)
    return RestCursorClient(api_key="test-key-123")


def test_launch_sends_client_agent_id(monkeypatch):
    seen = {}

    def handler(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        return FakeResp({"agent": {"id": seen["body"]["agentId"]},
                         "run": {"id": "run-1"}})

    c = make_client(monkeypatch, handler)
    res = c.launch("do things")
    assert seen["body"]["agentId"].startswith("bc-")
    assert seen["auth"] == "Bearer test-key-123"
    assert res["agent"]["id"] == seen["body"]["agentId"]


def test_launch_timeout_returns_unknown(monkeypatch):
    def handler(req, timeout=None):
        raise TimeoutError("timed out")

    c = make_client(monkeypatch, handler)
    res = c.launch("do things")
    assert res["status"] == "unknown"
    assert "cursor_list" in res["reconcile_hint"]


def test_model_auto_is_omitted(monkeypatch):
    seen = {}

    def handler(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return FakeResp({"agent": {}, "run": {}})

    c = make_client(monkeypatch, handler)
    c.launch("hi", model="Auto")
    assert "model" not in seen["body"]


def test_missing_key_is_helpful():
    c = RestCursorClient(api_key="")
    try:
        c.me()
    except CursorAPIError as exc:
        assert "CURSOR_API_KEY" in str(exc)
    else:
        raise AssertionError("should have raised")


def test_key_scrubbed_from_errors(monkeypatch):
    def handler(req, timeout=None):
        raise urllib.error.URLError("boom test-key-123 leaked?")

    c = make_client(monkeypatch, handler)
    try:
        c.me()
    except CursorAPIError as exc:
        assert "test-key-123" not in str(exc)
        assert "[REDACTED]" in str(exc)
    else:
        raise AssertionError("should have raised")


def test_repurl_scheme_normalized():
    data = {"git": {"branches": [{"repoUrl": "github.com/o/r",
                                  "branch": "cursor/x"}]}}
    out = _normalize_git(data)
    assert out["git"]["branches"][0]["repoUrl"] == "https://github.com/o/r"


def test_409_replay_returns_success_shape(monkeypatch):
    def handler(req, timeout=None):
        if req.method == "POST":
            err = urllib.error.HTTPError(
                req.full_url, 409, "Conflict", {}, io.BytesIO(
                    json.dumps({"code": "agent_id_conflict",
                                "message": "exists"}).encode()))
            raise err
        if req.full_url.endswith("/runs/run-9"):
            return FakeResp({"id": "run-9", "status": "FINISHED"})
        return FakeResp({"id": "bc-x", "latestRunId": "run-9"})

    c = make_client(monkeypatch, handler)
    res = c.launch("do things", idempotency_key="same-key")
    assert res["agent"]["id"] == "bc-x"
    assert res["run"]["id"] == "run-9"
    assert res["replayed"] is True


def test_launch_timeout_reconciles_existing_agent(monkeypatch):
    calls = []

    def handler(req, timeout=None):
        calls.append(req.method)
        if req.method == "POST":
            raise TimeoutError("timed out")
        if "/runs/" in req.full_url:
            return FakeResp({"id": "run-1", "status": "RUNNING"})
        return FakeResp({"id": "bc-y", "latestRunId": "run-1"})

    c = make_client(monkeypatch, handler)
    res = c.launch("do things")
    assert res["agent"]["id"] == "bc-y"
    assert res["reconciled_after_timeout"] is True


def test_launch_timeout_missing_agent_is_honest(monkeypatch):
    def handler(req, timeout=None):
        if req.method == "POST":
            raise TimeoutError("timed out")
        err = urllib.error.HTTPError(req.full_url, 404, "Not Found", {},
                                     io.BytesIO(b'{"message":"nope"}'))
        raise err

    c = make_client(monkeypatch, handler)
    try:
        c.launch("do things")
    except CursorAPIError as exc:
        assert "not created" in str(exc)
        assert "idempotency_key" in str(exc)
    else:
        raise AssertionError("should have raised")


def test_default_client_is_rest_without_explicit_transport(monkeypatch):
    import os
    from cursor_cloud_agents_mcp.client import default_client, RestCursorClient
    monkeypatch.delenv("CURSOR_TRANSPORT", raising=False)
    monkeypatch.delenv("CURSOR_SANDBOX", raising=False)
    assert isinstance(default_client(), RestCursorClient)
    monkeypatch.setenv("CURSOR_SANDBOX", "1")
    from cursor_cloud_agents_mcp.client import SandboxCursorClient
    assert isinstance(default_client(), SandboxCursorClient)


def test_non_timeout_oserror_is_not_timeout(monkeypatch):
    def handler(req, timeout=None):
        raise OSError("connection reset by peer")

    c = make_client(monkeypatch, handler)
    try:
        c.me()
    except CursorAPIError as exc:
        assert "timed out" not in str(exc).lower()
        assert "Network error" in str(exc)
    else:
        raise AssertionError("should have raised")
