"""Stage B: performance and rate-limit findings (each test reproduces one)."""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, call

PROFILE = rf"^/{FAKE_IG_ID}$"


@pytest.fixture
def sleeps(monkeypatch):
    from instagram_mcp import graph_client
    seen: list[float] = []
    monkeypatch.setattr(graph_client, "_sleep", seen.append)
    return seen


# B1: connections are reused instead of a new httpx.Client per request
def test_http_client_reused_across_requests(account, monkeypatch):
    from instagram_mcp import auth, graph_client
    made = []
    real = httpx.Client

    def counting(*a, **kw):
        made.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(graph_client.httpx, "Client", counting)
    account.route("GET", PROFILE, {"id": FAKE_IG_ID})
    for _ in range(5):
        client, _ = auth.client_for(None)
        client.get(FAKE_IG_ID)
    assert len(made) == 1


# B2: hourly throttles fail fast with usage info; short-burst limits are retried
@pytest.mark.parametrize("code", [4, 17, 32, 80002])
def test_hourly_throttle_fails_fast(account, sleeps, code):
    from instagram_mcp import server
    account.route("GET", PROFILE, (400, {"error": {"message": "limit reached", "code": code}}))
    res = call(server.get_profile)()
    assert res["ok"] is False and res["error_class"] == "rate_limited"
    assert len(account.calls("GET", PROFILE)) == 1 and sleeps == []
    assert "hour" in res["hint"]


def test_613_and_429_with_retry_after_are_retried(account, sleeps):
    from instagram_mcp import auth
    seq = [(400, {"error": {"message": "burst", "code": 613}}), {"id": FAKE_IG_ID}]
    account.route("GET", PROFILE, lambda req: seq.pop(0))
    client, _ = auth.client_for(None)
    assert client.get(FAKE_IG_ID)["id"] == FAKE_IG_ID and len(sleeps) == 1


def test_dev_mode_limit_never_retried(dm_account, sleeps):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$",
                     (500, {"error": {"message": "Timeout", "code": -2, "error_subcode": 2534084, "is_transient": True}}))
    call(server.list_conversations)()
    assert len(dm_account.calls("GET", r"/conversations$")) == 1


def test_all_walk_refused_when_usage_high(account):
    from instagram_mcp import graph_client, server
    graph_client.LAST_USAGE.update({"max_percent": 91})
    res = call(server.list_media)(limit="all")
    assert res["ok"] is False and res["error_class"] == "rate_limited" and "91" in res["error"]
    assert account.calls("GET", r"/media$") == []
    graph_client.LAST_USAGE.clear()


# B3: conversation listing has a short timeout, no retries and a total time budget
def test_conversation_listing_uses_short_single_attempt(dm_account, monkeypatch, sleeps):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", httpx.ReadTimeout("slow"))
    res = call(server.list_conversations)()
    assert res["ok"] is False
    assert len(dm_account.calls("GET", r"/conversations$")) == 1  # first page not retried 3x
    assert server.DM_LIST_TIMEOUT_S <= 45


def test_conversation_listing_time_budget(dm_account, monkeypatch):
    from instagram_mcp import server
    clock = {"t": 0.0}
    monkeypatch.setattr(server, "_monotonic", lambda: clock["t"])

    def conv(req):
        clock["t"] += 40  # every page takes 40 s
        n = int(req.params.get("after") or 0)
        return {"data": [{"id": f"c{n}", "updated_time": "t"}],
                "paging": {"cursors": {"after": str(n + 1)}, "next": "https://graph.facebook.com/n"}}

    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", conv)
    dm_account.route("GET", r"^/c\d+$", {"participants": {"data": []}})
    res = call(server.list_conversations)(limit=50)
    assert res["ok"] is True and res["count"] < 50 and res["next_cursor"]
    assert "time budget" in res["note"]


def test_conversation_ceiling_is_50():
    from instagram_mcp import server
    assert server.CEIL_CONVERSATIONS == 50


# B4: carousel videos share one deadline and backoff (live: the ?ids= batch query is
# deprecated in Graph v26.0+, so each child is checked individually per round)
def test_carousel_polls_children_per_round_with_backoff(account, monkeypatch):
    from instagram_mcp import server
    waits: list[float] = []
    monkeypatch.setattr(server.time, "sleep", waits.append)
    n = {"i": 0}

    def create(req):
        n["i"] += 1
        return {"id": f"K{n['i']}"}

    rounds = {"K1": 0, "K2": 0, "K3": 0}

    def status(req):
        cid = req.path.strip("/")
        if cid in rounds:
            rounds[cid] += 1
            return {"status_code": "FINISHED" if rounds[cid] >= 3 else "IN_PROGRESS"}
        return {"status_code": "FINISHED"}  # parent container

    account.route("POST", rf"^/{FAKE_IG_ID}/media$", create)
    account.route("GET", r"^/K\d+$", status)
    urls = [f"https://cdn.example.com/{i}.mp4" for i in range(3)]
    res = call(server.publish_carousel)(urls, prepare_only=True)
    assert res["ok"] is True, res
    assert rounds == {"K1": 3, "K2": 3, "K3": 3}  # 3 rounds, all children per round
    assert waits[:2] == [5, 10]  # one shared backoff, not 3 separate waits per round
    assert account.calls("GET", r"^/?$") == []  # no deprecated ?ids= query


def test_video_poll_cap(account, monkeypatch):
    from instagram_mcp import server
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "VIDEO_WAIT_S", 10_000)
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "IN_PROGRESS"})
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", prepare_only=True)
    assert res["error_class"] == "timeout"
    polls = len(account.calls("GET", r"^/C1$"))
    assert polls == server._max_polls()  # bounded, and scaled to the configured wait


# B5: top_posts caps insight lookups
def test_top_posts_caps_insight_lookups(account, monkeypatch):
    import json
    from datetime import datetime, timezone

    from instagram_mcp import server
    monkeypatch.setattr(server, "_utcnow", lambda: datetime(2026, 11, 13, 12, tzinfo=timezone.utc))
    posts = [{"id": f"P{i}", "timestamp": "2026-11-01T10:00:00+0000", "media_product_type": "FEED",
              "like_count": i} for i in range(400)]
    account.route("GET", rf"^/{FAKE_IG_ID}/media$", {"data": posts})

    def batch(req):
        items = json.loads(req.form["batch"])
        return [{"code": 200, "body": json.dumps({"data": [{"name": "reach", "values": [{"value": 1}]}]})} for _ in items]

    account.route("POST", r"^/?$", batch)
    res = call(server.top_posts)(since="2026-10-15", metric="reach")
    assert res["posts_considered"] == 400 and res["posts_ranked"] == server.TOP_POSTS_INSIGHT_CAP == 150
    assert len(account.calls("POST", r"^/?$")) == 3


# B6: nested replies are limited and truncation is flagged
def test_replies_limited_and_flagged(account):
    from instagram_mcp import server
    account.route("GET", r"^/555/comments$", {"data": [{"id": "c1", "replies": {"data": [{"id": str(i)} for i in range(50)]}}]})
    res = call(server.get_comments)("555", include_replies=True)
    assert "replies.limit(50)" in account.calls("GET", r"/comments$")[0].params["fields"]
    assert res["comments"][0]["replies_truncated"] is True


# B7: credentials are cached (no keychain call per tool call) and invalidated
def test_credentials_cached_and_invalidated(monkeypatch, tmp_path):
    from instagram_mcp import auth
    monkeypatch.setattr(auth, "_keychain_available", lambda: False)
    auth.add_account("main", "EAAtok000000000000000000000000", "17841400000000001")
    reads = []
    real = auth._secret_get
    monkeypatch.setattr(auth, "_secret_get", lambda a: reads.append(a) or real(a))
    for _ in range(5):
        auth.resolve(None)
    assert len(reads) == 1
    auth.add_account("main", "EAAnew000000000000000000000000", "17841400000000001")
    assert auth.resolve(None).access_token.startswith("EAAnew")


# B8: audit log rotates
def test_audit_log_rotates(monkeypatch, tmp_path):
    from instagram_mcp import audit
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_MAX_BYTES", 2000)
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(path))
    for _ in range(60):
        audit.record("x", execution_time_ms=1, io={"input": {"a": "b" * 50}, "output": {}})
    assert path.exists() and os.path.getsize(path) <= 2000 + 500
    assert (tmp_path / "audit.jsonl.1").exists()


def test_monotonic_hook_exists():
    from instagram_mcp import server
    assert server._monotonic() <= time.monotonic() + 1
