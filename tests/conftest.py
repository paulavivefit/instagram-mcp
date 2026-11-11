"""Shared fixtures: isolated env + an in-memory fake Graph API.

`fake_graph` intercepts every GraphClient request through httpx.MockTransport, so
tests exercise the real tool -> client -> HTTP code path without network or tokens.

    fake_graph.route("GET", r"/17841400000000000/media$", {"data": [...]})
    fake_graph.route("GET", r"/me/accounts$", lambda req: {...})
    fake_graph.requests  # every request seen (method, path, params, form)
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_TOKEN = "EAAfaketoken000000000000000000"
FAKE_IG_ID = "17841400000000000"
FAKE_PAGE_ID = "100000000000001"
FAKE_PAGE_TOKEN = "EAApagefaketoken0000000000000"

_MODULES = {"instagram_mcp", "instagram_mcp.audit", "instagram_mcp.auth",
            "instagram_mcp.graph_client", "instagram_mcp.validators",
            "instagram_mcp.server", "instagram_mcp.insights_metrics"}


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Every test runs against a fresh tmp accounts file + tmp audit log, no real token."""
    monkeypatch.setenv("INSTAGRAM_MCP_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(tmp_path / "audit.log.jsonl"))
    monkeypatch.delenv("INSTAGRAM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_MCP_IG_USER_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_MCP_DM_ENABLED", raising=False)
    # never really sleep in tests (retry backoff, video polling)
    import time
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    for mod in list(sys.modules):
        if mod in _MODULES:
            del sys.modules[mod]


@dataclass
class SeenRequest:
    method: str
    path: str
    params: dict[str, str]
    form: dict[str, str]

    @property
    def token(self) -> str | None:
        return self.params.get("access_token")


Responder = Any  # dict | callable(SeenRequest) -> dict | tuple[int, dict] | Exception


@dataclass
class FakeGraph:
    routes: list[tuple[str, re.Pattern[str], Responder]] = field(default_factory=list)
    requests: list[SeenRequest] = field(default_factory=list)

    def route(self, method: str, path_regex: str, responder: Responder) -> None:
        # later routes win, so a test can override a default
        self.routes.insert(0, (method.upper(), re.compile(path_regex), responder))

    def calls(self, method: str, path_regex: str) -> list[SeenRequest]:
        rx = re.compile(path_regex)
        return [r for r in self.requests if r.method == method.upper() and rx.search(r.path)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        # strip "/vNN.N" version prefix so routes are version-independent
        path = re.sub(r"^/v\d+\.\d+", "", request.url.path)
        params = dict(request.url.params)
        form = dict(parse_qsl(request.content.decode())) if request.content else {}
        seen = SeenRequest(request.method, path, params, form)
        self.requests.append(seen)
        for method, rx, responder in self.routes:
            if method == request.method and rx.search(path):
                out = responder(seen) if callable(responder) else responder
                if isinstance(out, Exception):
                    raise out
                status, body = out if isinstance(out, tuple) else (200, out)
                return httpx.Response(status, content=json.dumps(body).encode(),
                                      headers={"content-type": "application/json"})
        return httpx.Response(404, json={"error": {"message": f"no fake route for {request.method} {path}",
                                                   "code": 803}})


@pytest.fixture
def fake_graph(monkeypatch) -> FakeGraph:
    """Route every GraphClient through an in-memory fake Graph API."""
    from instagram_mcp import graph_client
    fake = FakeGraph()
    monkeypatch.setattr(graph_client, "DEFAULT_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def account(monkeypatch, fake_graph) -> FakeGraph:
    """A zero-config env account (no keychain) wired to the fake Graph API."""
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", FAKE_IG_ID)
    return fake_graph


@pytest.fixture
def dm_account(monkeypatch, account) -> FakeGraph:
    """Env account with DMs enabled and a Page linked to the IG account."""
    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    account.route("GET", r"^/me/accounts$", {"data": [
        {"id": "999", "access_token": "EAAother", "instagram_business_account": {"id": "1"}},
        {"id": FAKE_PAGE_ID, "access_token": FAKE_PAGE_TOKEN,
         "instagram_business_account": {"id": FAKE_IG_ID}},
    ]})
    return account


def call(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Call a FastMCP tool's underlying function regardless of wrapper shape."""
    return getattr(tool, "fn", tool)
