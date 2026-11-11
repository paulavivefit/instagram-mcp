"""Stage 3: retries/backoff, timeouts, usage headers, resumable video publishing."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import FAKE_IG_ID, call

PROFILE = rf"^/{FAKE_IG_ID}$"


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff sleeps instead of sleeping."""
    from instagram_mcp import graph_client
    seen: list[float] = []
    monkeypatch.setattr(graph_client, "_sleep", seen.append)
    return seen


def _sequence(*responses):
    """Responder returning the given responses in order (last one repeats)."""
    it = list(responses)

    def responder(req):
        return it.pop(0) if len(it) > 1 else it[0]
    return responder


THROTTLED = (400, {"error": {"message": "Application request limit reached", "code": 4}})
IG_THROTTLED = (400, {"error": {"message": "too many calls", "code": 80002}})
SERVER_ERR = (500, {"error": {"message": "An unexpected error has occurred", "code": 2, "is_transient": True}})
BAD_PARAM = (400, {"error": {"message": "(#100) bad", "code": 100}})
OK = {"id": FAKE_IG_ID, "username": "demo"}


# ---------------------------------- retries --------------------------------- #

@pytest.mark.parametrize("failure", [THROTTLED, IG_THROTTLED, SERVER_ERR])
def test_get_retries_transient_errors_then_succeeds(account, sleeps, failure):
    from instagram_mcp import auth
    account.route("GET", PROFILE, _sequence(failure, failure, OK))
    client, _ = auth.client_for(None)
    assert client.get(FAKE_IG_ID)["username"] == "demo"
    assert len(account.calls("GET", PROFILE)) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff


def test_get_gives_up_after_max_attempts(account, sleeps):
    from instagram_mcp import auth, graph_client
    account.route("GET", PROFILE, THROTTLED)
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.get(FAKE_IG_ID)
    assert exc.value.error_class == "rate_limited"
    assert len(account.calls("GET", PROFILE)) == 3


def test_non_transient_error_is_not_retried(account, sleeps):
    from instagram_mcp import auth, graph_client
    account.route("GET", PROFILE, BAD_PARAM)
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError):
        client.get(FAKE_IG_ID)
    assert len(account.calls("GET", PROFILE)) == 1 and sleeps == []


def test_get_retries_timeouts(account, sleeps):
    from instagram_mcp import auth
    account.route("GET", PROFILE, _sequence(httpx.ReadTimeout("slow"), OK))
    client, _ = auth.client_for(None)
    assert client.get(FAKE_IG_ID)["username"] == "demo"


def test_post_timeout_is_never_retried(account, sleeps):
    """A timed-out publish/send may have gone through: retrying could post twice."""
    from instagram_mcp import auth, graph_client
    account.route("POST", r"/media_publish$", httpx.ReadTimeout("slow"))
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.post(f"{FAKE_IG_ID}/media_publish", creation_id="1")
    assert exc.value.error_class == "timeout"
    assert len(account.calls("POST", r"/media_publish$")) == 1


def test_post_5xx_is_not_retried_but_throttle_is(account, sleeps):
    from instagram_mcp import auth, graph_client
    client, _ = auth.client_for(None)
    account.route("POST", r"/comments_a$", SERVER_ERR)
    with pytest.raises(graph_client.GraphAPIError):
        client.post("1/comments_a", message="x")
    assert len(account.calls("POST", r"/comments_a$")) == 1
    # throttling means Meta rejected the request without processing it -> safe to retry
    account.route("POST", r"/comments_b$", _sequence(THROTTLED, {"id": "c1"}))
    assert client.post("1/comments_b", message="x")["id"] == "c1"


def test_retry_after_header_is_honoured(account, sleeps, monkeypatch):
    from instagram_mcp import auth, graph_client
    real = graph_client.DEFAULT_TRANSPORT

    class FirstCall429(httpx.BaseTransport):
        n = 0

        def handle_request(self, request):
            FirstCall429.n += 1
            if FirstCall429.n == 1:
                return httpx.Response(429, headers={"Retry-After": "7"},
                                      content=json.dumps({"error": {"message": "slow down", "code": 4}}).encode())
            return real.handle_request(request)

    monkeypatch.setattr(graph_client, "DEFAULT_TRANSPORT", FirstCall429())
    account.route("GET", PROFILE, OK)
    client, _ = auth.client_for(None)
    assert client.get(FAKE_IG_ID)["username"] == "demo"
    assert sleeps == [7.0]


def test_default_timeout_is_60s(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_MCP_TIMEOUT", raising=False)
    from instagram_mcp import graph_client
    assert graph_client.DEFAULT_TIMEOUT == 60.0


# ------------------------------- usage headers ------------------------------- #

def test_usage_headers_are_recorded_and_shown_in_healthcheck(account, monkeypatch):
    from instagram_mcp import graph_client, server
    real = graph_client.DEFAULT_TRANSPORT

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            resp = real.handle_request(request)
            resp.headers["X-App-Usage"] = json.dumps({"call_count": 85, "total_time": 10, "total_cputime": 5})
            resp.headers["X-Business-Use-Case-Usage"] = json.dumps(
                {"123": [{"type": "instagram", "call_count": 20, "total_time": 3, "total_cputime": 2,
                          "estimated_time_to_regain_access": 0}]})
            return resp

    monkeypatch.setattr(graph_client, "DEFAULT_TRANSPORT", T())
    account.route("GET", PROFILE, OK)
    res = call(server.healthcheck)()
    assert res["api_usage"]["max_percent"] == 85
    assert "warning" in res["api_usage"]


# --------------------------- resumable video publish ------------------------- #

def test_video_wait_timeout_returns_resumable_container(account, monkeypatch):
    from instagram_mcp import server
    monkeypatch.setattr(server, "VIDEO_WAIT_S", 0)
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "IN_PROGRESS"})
    res = call(server.publish_reel)("https://example.com/v.mp4", caption="hi")
    assert res["ok"] is False and res["error_class"] == "timeout"
    assert res["container_id"] == "C1" and "publish_container" in res["hint"]
    assert account.calls("POST", r"/media_publish$") == []


def test_publish_container_publishes_when_finished(account):
    from instagram_mcp import server
    account.route("GET", r"^/C1$", {"status_code": "FINISHED"})
    account.route("POST", rf"^/{FAKE_IG_ID}/media_publish$", {"id": "M1"})
    res = call(server.publish_container)("C1")
    assert res["ok"] is True and res["media_id"] == "M1"


def test_publish_container_reports_still_processing(account):
    from instagram_mcp import server
    account.route("GET", r"^/C1$", {"status_code": "IN_PROGRESS", "status": "processing"})
    res = call(server.publish_container)("C1")
    assert res["ok"] is False and res["status_code"] == "IN_PROGRESS"
    assert account.calls("POST", r"/media_publish$") == []


def test_video_wait_default_is_300s(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_MCP_VIDEO_WAIT_S", raising=False)
    from instagram_mcp import server
    assert server.VIDEO_WAIT_S == 300
