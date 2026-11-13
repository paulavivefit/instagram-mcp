"""Stage U: issues reported from real Claude Desktop use (values from live probes, 2026-11-13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, call

INSIGHTS = rf"^/{FAKE_IG_ID}/insights$"
NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)


@pytest.fixture
def frozen(monkeypatch):
    from instagram_mcp import server
    monkeypatch.setattr(server, "_utcnow", lambda: NOW)
    return NOW


# ------------------------------- pure helpers -------------------------------- #

def test_pacific_date_of_meta_end_time():
    from instagram_mcp import insights_shape as S
    # live: winter end_time 08:00 UTC, summer 07:00 UTC = Pacific midnight -> previous day
    assert S.pacific_date("2024-12-01T08:00:00+0000") == "2024-11-30"
    assert S.pacific_date("2026-07-01T07:00:00+0000") == "2026-06-30"


def test_pacific_midnight():
    from instagram_mcp import insights_shape as S
    assert S.pacific_midnight("2024-12-01") == datetime(2024, 12, 1, 8, tzinfo=timezone.utc)
    assert S.pacific_midnight("2026-07-01") == datetime(2026, 7, 1, 7, tzinfo=timezone.utc)


def test_series_flattened_with_dates():
    from instagram_mcp import insights_shape as S
    vals = [{"value": 5, "end_time": "2024-12-02T08:00:00+0000"}]
    assert S.flatten_series(vals) == [{"date": "2024-12-01", "value": 5}]


def test_total_unavailable_rules():
    from instagram_mcp import insights_shape as S
    # live Dec 2024: views 0, total_interactions -5, accounts_engaged 0 while profile_views 6279
    window = {"views": 0, "total_interactions": -5, "accounts_engaged": 0, "profile_views": 6279}
    clean, unavailable = S.clean_window_totals(window, activity=123136)
    assert clean == {"views": None, "total_interactions": None, "accounts_engaged": None, "profile_views": 6279}
    assert set(unavailable) == {"views", "total_interactions", "accounts_engaged"}
    # a real zero stays zero when there was no activity at all
    clean, unavailable = S.clean_window_totals({"views": 0}, activity=0)
    assert clean == {"views": 0} and unavailable == {}


def test_media_quality_rules():
    from instagram_mcp import insights_shape as S
    # live: 2023 reel reach 185 / views 45326; 2017 feed reach 6 / likes 94 / views 0
    m, warn = S.media_quality("REELS", {"reach": 185, "views": 45326, "likes": 922})
    assert m["reach"] == 185 and any("reach" in w for w in warn)
    m, warn = S.media_quality("FEED", {"reach": 6, "views": 0, "likes": 94})
    assert m["views"] is None and any("reach" in w for w in warn)
    m, warn = S.media_quality("REELS", {"reach": 4848, "views": 8523, "likes": 142})
    assert warn == [] and m["views"] == 8523


def test_audience_sorted_with_percent():
    from instagram_mcp import insights_shape as S
    rows = [{"name": "follower_demographics", "total_value": {"breakdowns": [{"dimension_keys": ["country"], "results": [
        {"dimension_values": ["NO"], "value": 289}, {"dimension_values": ["PL"], "value": 43000},
        {"dimension_values": ["DE"], "value": 1856}]}]}}]
    out = S.audience_breakdown(rows)
    assert [r["key"] for r in out] == ["PL", "DE", "NO"]
    assert out[0]["value"] == 43000 and out[0]["percent"] == pytest.approx(95.3, abs=0.1)


# ---------------------------- account insights ------------------------------- #

def _window_responder(totals: dict, daily: int = 1):
    """Meta-like fake (both edges inclusive, see _meta_like_daily) with fixed totals per window."""
    def responder(req):
        names = req.params["metric"].split(",")
        if req.params.get("metric_type") == "total_value":
            return {"data": [{"name": n, "title": "Polski tytuł", "description": "opis", "id": "x",
                              "total_value": {"value": totals.get(n, 0)}} for n in names]}
        rows = _meta_like_daily(req)["data"]
        for r in rows:
            r.update({"title": "t", "description": "d", "id": "x"})
            for v in r["values"]:
                v["value"] = daily
        return {"data": rows}
    return responder

def test_account_insights_flat_metrics_and_nulls(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _window_responder({"views": 0, "profile_views": 6279, "total_interactions": -5}))
    res = call(server.get_account_insights)(metrics="reach,views,profile_views,total_interactions",
                                            since="2024-12-01", until="2024-12-10")
    assert res["ok"] is True, res
    m = res["metrics"]
    assert m["views"] is None and m["total_interactions"] is None and m["profile_views"] == 6279
    assert set(res["unavailable"]) == {"views", "total_interactions"}
    assert m["reach"][0]["date"] == "2024-12-01" and len(m["reach"]) == 10
    assert "insights" not in res and "Polski" not in str(res)


def test_account_insights_windows_aligned_to_pacific_midnight(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _window_responder({}))
    call(server.get_account_insights)(metrics="reach", since="2024-12-01", until="2024-12-03")
    c = account.calls("GET", INSIGHTS)[0]
    # Meta includes both edges, so requests start 1 s after Pacific midnight
    assert int(c.params["since"]) == int(datetime(2024, 12, 1, 8, tzinfo=timezone.utc).timestamp()) + 1
    assert int(c.params["until"]) == int(datetime(2024, 12, 4, 8, tzinfo=timezone.utc).timestamp())


def test_account_insights_raw_option(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _window_responder({"views": 5}))
    res = call(server.get_account_insights)(metrics="views", since="2026-11-01", until="2026-11-05", raw=True)
    assert res["raw"] and res["raw"][0]["name"] == "views"


def test_two_year_error_gives_earliest_date(account, frozen):
    from instagram_mcp import server
    res = call(server.get_account_insights)(since="2024-01-01")
    assert res["ok"] is False and res["error_class"] == "validation"
    earliest = (NOW - timedelta(days=729)).date()
    assert str(earliest + timedelta(days=1)) in res["error"] or str(earliest) in res["error"]


# ----------------------------- insights_summary ------------------------------ #

def test_summary_periods_do_not_overlap(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _window_responder({"views": 10, "reach": 10}))
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"followers_count": 5})
    res = call(server.insights_summary)(period="last_7_days")
    assert res["ok"] is True, res
    cur, prev = res["current_range"], res["previous_range"]
    assert prev[1] < cur[0]  # inclusive date ranges, no shared day
    assert (datetime.fromisoformat(cur[0]) - datetime.fromisoformat(prev[1])).days == 1


def test_summary_change_pct_none_when_unavailable(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _window_responder({"views": 0, "reach": 100, "profile_views": 50}))
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"followers_count": 5})
    res = call(server.insights_summary)(period="last_7_days")
    assert res["current"]["views"] is None and res["change_pct"]["views"] is None


# ------------------------------ media insights ------------------------------- #

def test_media_insights_flat_with_warning(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789$", {"media_product_type": "REELS", "media_type": "VIDEO"})
    account.route("GET", r"^/1789/insights$", {"data": [
        {"name": "reach", "title": "Zasięg", "values": [{"value": 185}]},
        {"name": "views", "values": [{"value": 45326}]}, {"name": "likes", "values": [{"value": 922}]}]})
    res = call(server.get_media_insights)("1789")
    assert res["metrics"] == {"reach": 185, "views": 45326, "likes": 922}
    assert res["warnings"] and "Zasięg" not in str(res)


def test_old_post_insights_error_has_hint(account):
    from instagram_mcp import server
    account.route("GET", r"^/1789$", {"media_product_type": "FEED"})
    account.route("GET", r"^/1789/insights$", (400, {"error": {"message": "Invalid parameter", "code": 100,
                                                               "error_subcode": 2108006}}))
    res = call(server.get_media_insights)("1789")
    assert res["ok"] is False and "older" in res["hint"].lower()


# ------------------------------ audience ------------------------------------ #

def test_audience_sorted_and_engaged_default_timeframe(account):
    from instagram_mcp import server

    def responder(req):
        return {"data": [{"name": req.params["metric"], "total_value": {"breakdowns": [{"results": [
            {"dimension_values": ["NO"], "value": 1}, {"dimension_values": ["PL"], "value": 9}]}]}}]}

    account.route("GET", INSIGHTS, responder)
    res = call(server.get_audience_insights)(breakdown="country")
    assert [r["key"] for r in res["breakdowns"]["country"]] == ["PL", "NO"]
    call(server.get_audience_insights)(metric="engaged_audience_demographics", breakdown="age")
    assert account.calls("GET", INSIGHTS)[-1].params["timeframe"] == "this_month"


# -------------------------------- top_posts ---------------------------------- #

def test_top_posts_by_likes_needs_no_insights_and_no_2_year_limit(account, frozen):
    from instagram_mcp import server
    posts = [{"id": f"P{i}", "timestamp": f"20{16 + i % 3}-05-01T10:00:00+0000", "like_count": i,
              "comments_count": 0, "media_product_type": "FEED"} for i in range(9)]
    account.route("GET", rf"^/{FAKE_IG_ID}/media$", {"data": sorted(posts, key=lambda p: p["timestamp"], reverse=True)})
    res = call(server.top_posts)(since="2016-01-01", until="2018-12-31", metric="likes", n=3)
    assert res["ok"] is True, res
    assert [p["id"] for p in res["posts"]] == ["P8", "P7", "P6"]
    assert account.calls("POST", r"^/?$") == []


# ------------------------------ other errors --------------------------------- #

def test_business_discovery_unknown_user_hint(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}$", (400, {"error": {"message": "Invalid user id", "code": 110,
                                                                "error_subcode": 2207013, "type": "OAuthException"}}))
    res = call(server.business_discovery)("nosuchuser")
    assert res["error_class"] == "invalid_param" and "Business/Creator" in res["hint"]


def test_unknown_account_label_is_validation(account, monkeypatch, tmp_path):
    from instagram_mcp import server
    res = call(server.get_profile)(account="nope")
    assert res["ok"] is False and res["error_class"] == "validation"


# ------------------------------- consistency --------------------------------- #

def test_replies_and_children_are_flat_lists(account):
    from instagram_mcp import server
    account.route("GET", r"^/555/comments$", {"data": [{"id": "c1", "replies": {"data": [{"id": "r1"}]}}]})
    res = call(server.get_comments)("555", include_replies=True)
    assert res["comments"][0]["replies"] == [{"id": "r1"}]
    account.route("GET", r"^/777$", {"id": "777", "children": {"data": [{"id": "k1"}]}})
    assert call(server.get_media)("777")["media"]["children"] == [{"id": "k1"}]


def test_account_info_removed():
    from instagram_mcp import server
    assert not hasattr(server, "account_info")


# ------------------------------- healthcheck --------------------------------- #

def test_healthcheck_reports_features(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_IG_ID}$", {"id": FAKE_IG_ID, "username": "demo"})
    dm_account.route("GET", r"^/debug_token$", {"data": {"is_valid": True, "scopes": ["instagram_basic"],
                                                         "data_access_expires_at": 1802186656}})
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", (400, {"error": {
        "message": "Timeout", "code": -2, "error_subcode": 2534084}}))
    call(server.list_conversations)()
    res = call(server.healthcheck)()
    f = res["features"]
    assert f["dm"]["status"] == "dev_mode_limit"
    assert "development" in f["app_mode"]
    assert "Public Content Access" in f["hashtag_search"]
    assert res["token"]["scopes"] == ["instagram_basic"] and res["token"]["data_access_expires"] == "2027-02-09"


def test_blocked_features_are_described_upfront():
    import asyncio

    from instagram_mcp import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("search_hashtag", "get_hashtag_media"):
        assert "Public Content Access" in tools[name].description
    assert "development mode" in tools["list_conversations"].description.lower()


def test_windows_never_create_tiny_dst_slivers():
    """30 Pacific days across the November DST change are 30 d + 1 h > Meta's 30-day limit."""
    from instagram_mcp import insights_shape as S
    from instagram_mcp import server
    start, end = S.pacific_midnight("2026-10-12"), S.pacific_midnight("2026-11-11")
    wins = server._windows(start, end)
    assert all((b - a).total_seconds() <= 30 * 86400 for a, b in wins)
    assert all((b - a).total_seconds() >= 86400 for a, b in wins)  # whole days only
    assert wins[0][0] == start and wins[-1][1] == end
    # every boundary is a Pacific midnight
    assert all(S.pacific_midnight(S.pacific_today(a)) == a for a, _ in wins)


def _meta_like_daily(req):
    """Meta (measured live 2026-11-13): returns every daily value whose end_time (a Pacific
    midnight) lies in [since, until] - BOTH edges inclusive - and totals over the same days."""
    from instagram_mcp import insights_shape as S
    since, until = int(req.params["since"]), int(req.params["until"])
    day = S.pacific_today(datetime.fromtimestamp(since, timezone.utc) - timedelta(days=1))
    ends = []
    while True:
        m = S.pacific_midnight(day)
        if m.timestamp() > until:
            break
        if m.timestamp() >= since:
            ends.append(m)
        day += timedelta(days=1)
    names = req.params["metric"].split(",")
    if req.params.get("metric_type") == "total_value":
        return {"data": [{"name": n, "total_value": {"value": 10 * len(ends)}} for n in names]}
    return {"data": [{"name": n, "values": [{"value": 1, "end_time": e.strftime("%Y-%m-%dT%H:%M:%S+0000")}
                                            for e in ends]} for n in names]}


def test_range_has_exact_days_no_boundary_duplicates(account, frozen):
    from instagram_mcp import server
    account.route("GET", INSIGHTS, _meta_like_daily)
    res = call(server.get_account_insights)(metrics="reach,profile_views", since="2024-12-01", until="2025-02-28")
    reach = res["metrics"]["reach"]
    dates = [v["date"] for v in reach]
    assert dates[0] == "2024-12-01" and dates[-1] == "2025-02-28"
    assert len(dates) == 90 and len(set(dates)) == 90  # no day twice across 30-day windows
    assert res["metrics"]["profile_views"] == 10 * 90  # totals cover exactly the 90 days
