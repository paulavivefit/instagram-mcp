"""Stage 1: insights tools send combinations Meta actually accepts (verified live 2026-11-11)."""

from __future__ import annotations

from tests.conftest import FAKE_IG_ID, call

INSIGHTS = rf"^/{FAKE_IG_ID}/insights$"


def _echo_metrics(req):
    return {"data": [{"name": m, "period": req.params.get("period"), "values": [{"value": 1}]}
                     for m in req.params["metric"].split(",")]}


# ------------------------------ account insights ----------------------------- #

def test_account_insights_default_splits_time_series_and_total_value(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _echo_metrics)
    res = call(server.get_account_insights)()
    assert res["ok"] is True, res
    calls = account.calls("GET", INSIGHTS)
    ts = [c for c in calls if "metric_type" not in c.params]
    tv = [c for c in calls if c.params.get("metric_type") == "total_value"]
    assert len(ts) == 1 and len(tv) == 1
    assert set(ts[0].params["metric"].split(",")) == {"reach", "follower_count"}
    assert set(tv[0].params["metric"].split(",")) == {"views", "profile_views",
                                                       "accounts_engaged", "total_interactions"}
    assert tv[0].params["period"] == "day"  # total_value metrics only accept period=day
    assert "impressions" not in ",".join(c.params["metric"] for c in calls)
    names = {d["name"] for d in res["insights"]}
    assert names == {"reach", "follower_count", "views", "profile_views",
                     "accounts_engaged", "total_interactions"}


def test_account_insights_custom_total_value_metric_only(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _echo_metrics)
    res = call(server.get_account_insights)(metrics="website_clicks")
    assert res["ok"] is True, res
    calls = account.calls("GET", INSIGHTS)
    assert len(calls) == 1
    assert calls[0].params["metric_type"] == "total_value"


def test_account_insights_period_applies_to_time_series_only(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _echo_metrics)
    res = call(server.get_account_insights)(metrics="reach,views", period="week")
    assert res["ok"] is True, res
    by_type = {c.params.get("metric_type"): c.params["period"] for c in account.calls("GET", INSIGHTS)}
    assert by_type == {None: "week", "total_value": "day"}


def test_account_insights_partial_failure_reports_errors(account):
    from instagram_mcp import server

    def responder(req):
        if req.params.get("metric_type") == "total_value":
            return (400, {"error": {"message": "(#100) bad metric", "code": 100}})
        return _echo_metrics(req)

    account.route("GET", INSIGHTS, responder)
    res = call(server.get_account_insights)()
    assert res["ok"] is True
    assert {d["name"] for d in res["insights"]} == {"reach", "follower_count"}
    assert len(res["errors"]) == 1 and "bad metric" in res["errors"][0]["error"]


def test_account_insights_all_fail_is_not_ok(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, (400, {"error": {"message": "(#100) nope", "code": 100}}))
    res = call(server.get_account_insights)()
    assert res["ok"] is False


# ------------------------------ audience insights ---------------------------- #

def test_audience_insights_one_call_per_breakdown(account):
    from instagram_mcp import server

    def responder(req):
        return {"data": [{"name": "follower_demographics",
                          "total_value": {"breakdowns": [{"dimension_keys": [req.params["breakdown"]]}]}}]}

    account.route("GET", INSIGHTS, responder)
    res = call(server.get_audience_insights)()
    assert res["ok"] is True, res
    calls = account.calls("GET", INSIGHTS)
    assert sorted(c.params["breakdown"] for c in calls) == ["age", "city", "country", "gender"]
    assert all("," not in c.params["breakdown"] for c in calls)
    assert set(res["breakdowns"]) == {"age", "gender", "country", "city"}
    assert "timeframe" not in calls[0].params


def test_audience_insights_single_breakdown_and_timeframe(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, {"data": [{"name": "engaged_audience_demographics"}]})
    res = call(server.get_audience_insights)(metric="engaged_audience_demographics",
                                             breakdown="age", timeframe="this_month")
    assert res["ok"] is True, res
    (c,) = account.calls("GET", INSIGHTS)
    assert c.params["breakdown"] == "age" and c.params["timeframe"] == "this_month"


def test_audience_insights_unknown_error_gets_hint(account):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, (500, {"error": {"message": "An unknown error has occurred.", "code": 1}}))
    res = call(server.get_audience_insights)(breakdown="age")
    assert res["ok"] is False
    assert "hint" in res and "100 followers" in res["hint"]


# ------------------------------- media insights ------------------------------ #

def _media(product_type, media_type):
    return {"id": "1789", "media_product_type": product_type, "media_type": media_type}


def test_media_insights_reel_gets_reel_metrics(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789$", _media("REELS", "VIDEO"))
    account.route("GET", r"^/1789/insights$", _echo_metrics)
    res = call(server.get_media_insights)("1789")
    assert res["ok"] is True, res
    metric = account.calls("GET", r"^/1789/insights$")[0].params["metric"]
    assert "ig_reels_avg_watch_time" in metric and "views" in metric
    assert res["media_product_type"] == "REELS"


def test_media_insights_feed_post_has_no_reel_metrics(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789$", _media("FEED", "CAROUSEL_ALBUM"))
    account.route("GET", r"^/1789/insights$", _echo_metrics)
    call(server.get_media_insights)("1789")
    metric = account.calls("GET", r"^/1789/insights$")[0].params["metric"]
    assert "ig_reels" not in metric
    assert set(metric.split(",")) >= {"reach", "likes", "comments", "shares", "saved", "views"}


def test_media_insights_story_metrics(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789$", _media("STORY", "IMAGE"))
    account.route("GET", r"^/1789/insights$", _echo_metrics)
    call(server.get_media_insights)("1789")
    metric = account.calls("GET", r"^/1789/insights$")[0].params["metric"]
    assert "replies" in metric and "likes" not in metric


def test_media_insights_explicit_metrics_skip_lookup(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789/insights$", _echo_metrics)
    res = call(server.get_media_insights)("1789", metrics="reach")
    assert res["ok"] is True, res
    assert account.calls("GET", r"^/1789$") == []
