"""Stage 4: insights over date ranges, top_posts, insights_summary (rules verified live)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import FAKE_IG_ID, call

INSIGHTS = rf"^/{FAKE_IG_ID}/insights$"
MEDIA = rf"^/{FAKE_IG_ID}/media$"
DAY = 86400


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin 'now' used by the tools to 2026-11-11 12:00 UTC."""
    from instagram_mcp import server
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "_utcnow", lambda: now)
    return now


def _insights_responder(req):
    """Meta-like fake: values per Pacific day, both edges inclusive (see test_user_feedback)."""
    from tests.test_user_feedback import _meta_like_daily
    out = _meta_like_daily(req)
    assert int(req.params["until"]) - int(req.params["since"]) <= 30 * DAY, "Meta rejects windows longer than 30 days"
    return out


def _insights_responder_old(req):
    since, until = int(req.params["since"]), int(req.params["until"])
    assert until - since <= 30 * DAY, "Meta rejects windows longer than 30 days"
    out = []
    for m in req.params["metric"].split(","):
        if req.params.get("metric_type") == "total_value":
            out.append({"name": m, "period": "day", "total_value": {"value": 10}})
        else:
            days = (until - since) // DAY
            out.append({"name": m, "period": "day",
                        "values": [{"value": 1, "end_time": datetime.fromtimestamp(since + (i + 1) * DAY, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")} for i in range(days)]})
    return {"data": out}


# --------------------------- account insights ranges -------------------------- #

def test_range_over_30_days_is_chunked(account, frozen_now):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _insights_responder)
    res = call(server.get_account_insights)(metrics="reach,views", since="2026-08-01", until="2026-10-31")
    assert res["ok"] is True, res
    calls = account.calls("GET", INSIGHTS)
    windows = sorted({(int(c.params["since"]), int(c.params["until"])) for c in calls})
    assert len(windows) == 4  # 92 days -> 30+30+30+2
    assert all(u - s <= 30 * DAY for s, u in windows)
    # windows start 1 s after Pacific midnight (Meta includes both edges): 2026-08-01 00:00 PDT = 07:00 UTC
    assert windows[0][0] == int(datetime(2026, 8, 1, 7, tzinfo=timezone.utc).timestamp()) + 1
    m = res["metrics"]
    assert len(m["reach"]) == 92  # daily values stitched in order
    assert m["reach"][0]["date"] == "2026-08-01" and m["reach"][-1]["date"] == "2026-10-31"
    assert m["views"] == 10 * 92  # summed over 4 windows, exactly the 92 requested days
    assert res["complete"] is True


def test_follower_count_only_requested_for_last_30_days(account, frozen_now):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _insights_responder)
    res = call(server.get_account_insights)(metrics="follower_count", since="2026-08-01", until="2026-11-10")
    assert res["ok"] is True, res
    for c in account.calls("GET", INSIGHTS):
        assert int(c.params["since"]) >= int((frozen_now - timedelta(days=31)).timestamp())
    assert "follower_count" in res["note"]


def test_range_validation(account, frozen_now):
    from instagram_mcp import server
    bad = [dict(since="2026-11-10", until="2026-11-01"),
           dict(since="2024-01-01", until="2024-02-01"),  # older than 2 years
           dict(since="soon")]
    for kw in bad:
        res = call(server.get_account_insights)(**kw)
        assert res["ok"] is False and res["error_class"] == "validation", kw


def test_until_in_future_is_clamped_to_now(account, frozen_now):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _insights_responder)
    call(server.get_account_insights)(metrics="views", since="2026-11-01", until="2027-01-01")
    assert max(int(c.params["until"]) for c in account.calls("GET", INSIGHTS)) <= int(frozen_now.timestamp())


def test_no_range_keeps_previous_behaviour(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _insights_responder_no_range)
    res = call(server.get_account_insights)(metrics="views")
    assert res["ok"] is True
    assert "since" not in account.calls("GET", INSIGHTS)[0].params


def _insights_responder_no_range(req):
    return {"data": [{"name": m, "total_value": {"value": 1}} for m in req.params["metric"].split(",")]}


# ---------------------------------- top_posts --------------------------------- #

def _posts():
    base = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    kinds = [("FEED", "IMAGE"), ("REELS", "VIDEO"), ("FEED", "CAROUSEL_ALBUM")]
    return [{"id": f"P{i}", "media_product_type": kinds[i % 3][0], "media_type": kinds[i % 3][1],
             "permalink": f"https://instagram.com/p/{i}", "caption": "caption " * 40,
             "timestamp": (base - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
            for i in range(60)]


def _batch_responder(fail_ids=()):
    def responder(req):
        batch = json.loads(req.form["batch"])
        assert len(batch) <= 50
        out = []
        for item in batch:
            mid = item["relative_url"].split("/")[0]
            if mid in fail_ids:
                out.append({"code": 400, "body": json.dumps({"error": {"message": "nope", "code": 100}})})
                continue
            n = int(mid[1:])
            names = item["relative_url"].split("metric=")[1].split("&")[0].split(",")
            out.append({"code": 200, "body": json.dumps({"data": [
                {"name": m, "values": [{"value": n * (2 if m == "reach" else 1)}]} for m in names]})})
        return out
    return responder


def test_top_posts_sorts_by_metric_using_batch(account, frozen_now):
    from instagram_mcp import server
    account.route("GET", MEDIA, {"data": _posts()})
    account.route("POST", r"^/?$", _batch_responder())
    res = call(server.top_posts)(since="2026-09-01", until="2026-10-31", metric="reach", n=5)
    assert res["ok"] is True, res
    assert [p["id"] for p in res["posts"]] == ["P59", "P58", "P57", "P56", "P55"]
    assert res["posts"][0]["metrics"]["reach"] == 118
    assert len(res["posts"][0]["caption"]) <= 120
    assert len(account.calls("POST", r"^/?$")) == 2  # 60 posts -> 2 batches of <=50
    assert res["posts_considered"] == 60


def test_top_posts_filters_media_type_and_reports_errors(account, frozen_now):
    from instagram_mcp import server
    account.route("GET", MEDIA, {"data": _posts()})
    account.route("POST", r"^/?$", _batch_responder(fail_ids={"P58"}))
    res = call(server.top_posts)(since="2026-09-01", until="2026-10-31", metric="views", n=3,
                                 media_type="REELS")
    assert res["ok"] is True, res
    assert all(p["media_product_type"] == "REELS" for p in res["posts"])
    assert [p["id"] for p in res["posts"]] == ["P55", "P52", "P49"]
    assert res["errors"] and res["errors"][0]["id"] == "P58"


def test_top_posts_rejects_unknown_metric(account, frozen_now):
    from instagram_mcp import server
    res = call(server.top_posts)(metric="impressions")
    assert res["ok"] is False and res["error_class"] == "validation"


# ------------------------------- insights_summary ------------------------------ #

def test_insights_summary_compares_with_previous_period(account, frozen_now):
    from instagram_mcp import server

    def responder(req):
        since, until = int(req.params["since"]), int(req.params["until"])
        current = since >= int((frozen_now - timedelta(days=30)).timestamp()) - DAY
        days = round((until - since) / DAY)  # Meta returns one total per requested window
        val = days * (10 if current else 5)
        out = []
        for m in req.params["metric"].split(","):
            if req.params.get("metric_type") == "total_value":
                out.append({"name": m, "total_value": {"value": val}})
            else:
                out.append({"name": m, "values": [{"value": 5}, {"value": 7}]})
        return {"data": out}

    account.route("GET", INSIGHTS, responder)
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"followers_count": 5000, "media_count": 10})
    res = call(server.insights_summary)(period="last_30_days")
    assert res["ok"] is True, res
    assert res["current"]["views"] == 300 and res["previous"]["views"] == 150  # 30 days each, no overlap
    assert res["change_pct"]["views"] == 100.0
    assert res["followers_now"] == 5000
    assert res["new_followers"] == 12


def test_insights_summary_rejects_unknown_period(account, frozen_now):
    from instagram_mcp import server
    res = call(server.insights_summary)(period="forever")
    assert res["ok"] is False and res["error_class"] == "validation"
