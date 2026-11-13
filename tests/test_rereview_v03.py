"""Stage G: findings of the independent pre-release review (each test reproduces one)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, FAKE_TOKEN, call
from tests.test_user_feedback import _meta_like_daily

INSIGHTS = rf"^/{FAKE_IG_ID}/insights$"


def _freeze(monkeypatch, when):
    from instagram_mcp import server
    monkeypatch.setattr(server, "_utcnow", lambda: when)


# 1. the 25-hour autumn DST day must not be dropped
def test_autumn_dst_day_included(account, monkeypatch):
    from instagram_mcp import server
    _freeze(monkeypatch, datetime(2025, 12, 20, 12, tzinfo=timezone.utc))
    account.route("GET", INSIGHTS, _meta_like_daily)
    one = call(server.get_account_insights)(metrics="reach", since="2025-11-02", until="2025-11-02")
    assert [v["date"] for v in one["metrics"]["reach"]] == ["2025-11-02"]
    month = call(server.get_account_insights)(metrics="reach", since="2025-10-04", until="2025-11-02")
    assert len(month["metrics"]["reach"]) == 30 and month["until"] == "2025-11-02"


def test_spring_dst_until_reported_correctly(account, monkeypatch):
    from instagram_mcp import server
    _freeze(monkeypatch, datetime(2025, 12, 20, 12, tzinfo=timezone.utc))
    account.route("GET", INSIGHTS, _meta_like_daily)
    res = call(server.get_account_insights)(metrics="reach", since="2025-03-01", until="2025-03-09")
    assert res["until"] == "2025-03-09" and len(res["metrics"]["reach"]) == 9


# 2. follower_count window is exactly 30 Pacific days, also after spring DST
def test_follower_count_30_days_after_spring_dst(account, monkeypatch):
    from instagram_mcp import server
    _freeze(monkeypatch, datetime(2026, 3, 20, 18, tzinfo=timezone.utc))
    account.route("GET", INSIGHTS, _meta_like_daily)
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"followers_count": 1})
    res = call(server.insights_summary)(period="last_90_days")
    assert res["new_followers"] == 30


# 3. a timed-out second part of a DM may have been delivered
def test_partial_dm_timeout_keeps_uncertain_hint(dm_account):
    from instagram_mcp import server

    def responder(req):
        if "attachment" in req.form["message"]:
            return httpx.ReadTimeout("slow")
        return {"message_id": "m1"}

    dm_account.route("POST", rf"^/{FAKE_PAGE_ID}/messages$", responder)
    res = call(server.send_message)("921", "hi", image_url="https://cdn.example.com/x.jpg")
    assert res["sent_before_error"] == 1
    assert "may have gone through" in res["hint"] and "only retry" not in res["hint"]


# 4. genuine zeros stay zero; only metrics with known collection gaps are nulled
def test_genuine_zero_not_nulled():
    from instagram_mcp import insights_shape as S
    clean, bad = S.clean_window_totals({"website_clicks": 0, "replies": 0, "profile_views": 50, "views": 0}, 1000)
    assert clean["website_clicks"] == 0 and clean["replies"] == 0
    assert clean["views"] is None and set(bad) == {"views"}
    clean, bad = S.clean_window_totals({"shares": -2}, 0)  # negative is never real
    assert clean["shares"] is None


# 5. synthetic cursor never loses a post sharing a second with another
def test_synthetic_cursor_with_same_second_posts(account):
    from instagram_mcp import server
    ts = "2026-11-10T10:00:00+0000"
    posts = [{"id": "1", "timestamp": ts}, {"id": "2", "timestamp": ts}, {"id": "3", "timestamp": "2026-11-09T10:00:00+0000"},
             {"id": "0", "timestamp": "2026-11-12T10:00:00+0000"}]
    posts.sort(key=lambda p: p["timestamp"], reverse=True)
    account.route("GET", rf"^/{FAKE_IG_ID}/media$", {"data": posts})
    seen, cur = [], None
    for _ in range(10):
        r = call(server.list_media)(limit=1, until="2026-11-11", after=cur)
        seen += [m["id"] for m in r["media"]]
        cur = r.get("next_cursor")
        if not cur:
            break
    assert seen == ["1", "2", "3"]


# 6. healthcheck never puts the token in a URL
def test_healthcheck_token_not_in_url(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"username": "demo"})
    account.route("GET", r"^/me/permissions$", {"data": [{"permission": "instagram_basic", "status": "granted"},
                                                         {"permission": "x", "status": "declined"}]})
    res = call(server.healthcheck)()
    assert res["token"]["scopes"] == ["instagram_basic"]
    assert all(FAKE_TOKEN not in str(r.params) for r in account.requests)


# 7. removing the default with an env account active falls back to env
def test_remove_default_falls_back_to_env(monkeypatch):
    from instagram_mcp import auth
    monkeypatch.setattr(auth, "_keychain_available", lambda: False)
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", FAKE_IG_ID)
    auth.add_account("a", "EAAtokA0000000000000000000000", "17841400000000001", make_default=True)
    auth.add_account("b", "EAAtokB0000000000000000000000", "17841400000000002")
    auth.remove_account("a")
    assert auth.resolve(None).label == "env"


# 8. a longer configured video wait is honoured (poll cap scales)
def test_poll_cap_scales_with_configured_wait(monkeypatch):
    from instagram_mcp import server
    monkeypatch.setattr(server, "VIDEO_WAIT_S", 1800)
    assert sum(server._poll_delay(n) for n in range(server._max_polls())) >= 1800
    monkeypatch.setattr(server, "VIDEO_WAIT_S", 300)
    assert server._max_polls() == 15
