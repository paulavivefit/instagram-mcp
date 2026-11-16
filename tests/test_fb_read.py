"""Read tools: posts, comments, tagged, scheduled, insights, summary, healthcheck."""

from __future__ import annotations

from tests.conftest import FB_PAGE_ID, call

POST = f"{FB_PAGE_ID}_100"


def _srv():
    from facebook_mcp import server
    return server


def _post(i, reactions=1, comments=2, shares=None, msg="hello"):
    p = {"id": f"{FB_PAGE_ID}_{i}", "message": msg, "created_time": "2026-11-10T10:00:00+0000",
         "reactions": {"data": [], "summary": {"total_count": reactions}},
         "comments": {"data": [], "summary": {"total_count": comments}}}
    if shares is not None:
        p["shares"] = {"count": shares}
    return p


def test_healthcheck_reports_permissions_and_lock(fb_account):
    fb_account.route("GET", r"^/me$", {"id": "1", "name": "Pa Pi"})
    fb_account.route("GET", r"^/me/permissions$", {"data": [
        {"permission": p, "status": "granted"} for p in
        ("pages_show_list", "pages_read_engagement", "pages_read_user_content", "pages_manage_posts")]})
    out = call(_srv().healthcheck)()
    assert out["ok"] and out["allowed_pages"] == [{"page_id": FB_PAGE_ID, "page_name": "Trening Fitness"}]
    assert out["missing_permissions"] == ["pages_manage_engagement", "read_insights"]
    assert "hint" in out and "(set)" in out["page_lock"]


def test_list_posts_flattens_counts_and_pages(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/posts$", {
        "data": [_post(1, shares=3), _post(2)],
        "paging": {"cursors": {"after": "CUR"}, "next": "https://x"}})
    out = call(_srv().list_posts)(limit=2, since="2026-11-01", until="2026-11-15")
    assert out["ok"] and out["count"] == 2 and out["next_cursor"] == "CUR"
    p = out["posts"][0]
    assert (p["reactions_count"], p["comments_count"], p["shares_count"], p["engagement"]) == (1, 2, 3, 6)
    assert out["posts"][1]["shares_count"] == 0
    req = fb_account.calls("GET", r"/posts$")[0]
    assert req.params["since"] == "1793491200" and int(req.params["until"]) > int(req.params["since"])


def test_list_posts_compact_truncates(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/posts$", {"data": [_post(1, msg="x" * 400)]})
    out = call(_srv().list_posts)(compact=True)
    assert out["posts"][0]["message_truncated"] and len(out["posts"][0]["message"]) == 151


def test_list_posts_bad_range(fb_account):
    out = call(_srv().list_posts)(since="2026-11-10", until="2026-11-01")
    assert out["error_class"] == "validation"


def test_list_posts_all_refused_when_quota_high(fb_account):
    from instagram_mcp import graph_client
    graph_client.LAST_USAGE["max_percent"] = 95
    try:
        out = call(_srv().list_posts)(limit="all")
    finally:
        graph_client.LAST_USAGE.clear()
    assert out["error_class"] == "rate_limited"


def test_get_post(fb_account):
    fb_account.route("GET", rf"^/{POST}$", {**_post(100), "attachments": {"data": [{"type": "photo"}]}})
    out = call(_srv().get_post)(post_id=POST)
    assert out["post"]["attachments"] == [{"type": "photo"}]


def test_get_comments_with_replies_and_author_note(fb_account):
    fb_account.route("GET", rf"^/{POST}/comments$", {"data": [
        {"id": "100_1", "message": "hi", "comments": {"data": [{"id": "100_2", "message": "re",
                                                                 "from": {"id": FB_PAGE_ID}}],
                                                      "paging": {"next": "n"}}},
    ]})
    out = call(_srv().get_comments)(post_id=POST, order="oldest")
    c = out["comments"][0]
    assert c["replies"][0]["id"] == "100_2" and c["more_replies"] is True
    assert "note" in out
    req = fb_account.calls("GET", r"/comments$")[0]
    assert req.params["order"] == "chronological" and req.params["filter"] == "toplevel"


def test_get_comments_no_note_when_authors_visible(fb_account):
    fb_account.route("GET", rf"^/{POST}/comments$", {"data": [{"id": "100_1", "from": {"id": "9"}}]})
    out = call(_srv().get_comments)(post_id=POST, include_replies=False)
    assert "note" not in out


def test_get_comment_and_replies(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_100$", {"id": POST})
    fb_account.route("GET", r"^/100_1$", {"id": "100_1", "message": "hi", "from": {"id": "9"}})
    fb_account.route("GET", r"^/100_1/comments$", {"data": [{"id": "100_3", "from": {"id": "9"}}]})
    assert call(_srv().get_comment)(comment_id="100_1")["comment"]["message"] == "hi"
    out = call(_srv().get_comment_replies)(comment_id="100_1")
    assert out["count"] == 1 and out["replies"][0]["id"] == "100_3"


def test_tagged_and_scheduled(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/tagged$", {"data": [{"id": "5_6", "message": "tag"}]})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/scheduled_posts$",
                     {"data": [{"id": f"{FB_PAGE_ID}_9", "scheduled_publish_time": 1800000000}]})
    t = call(_srv().list_tagged_posts)()
    assert t["count"] == 1 and "note" in t
    s = call(_srv().list_scheduled_posts)()
    assert s["posts"][0]["scheduled_publish_time"].startswith("2027-01-15")


def test_top_posts_by_engagement(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/posts$", {"data": [_post(1, 1, 1), _post(2, 50, 5), _post(3, 9, 0)]})
    out = call(_srv().top_posts)(top=2)
    assert [p["id"] for p in out["posts"]] == [f"{FB_PAGE_ID}_2", f"{FB_PAGE_ID}_3"]
    assert out["considered"] == 3


def test_top_posts_by_views_uses_insights(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/posts$", {"data": [_post(1), _post(2)]})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_1/insights$",
                     {"data": [{"name": "post_media_view", "values": [{"value": 10}]}]})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_2/insights$",
                     {"data": [{"name": "post_media_view", "values": [{"value": 99}]}]})
    out = call(_srv().top_posts)(metric="views")
    assert out["posts"][0]["id"] == f"{FB_PAGE_ID}_2" and out["posts"][0]["views"] == 99


def test_page_insights_series_totals_and_breakdown(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/insights$", lambda r: {"data": [
        {"name": "page_follows_country", "period": "day",
         "values": [{"value": {"DE": 5, "PL": 50}, "end_time": "2026-11-11T08:00:00+0000"}]}]}
        if r.params["metric"] == "page_follows_country" else {"data": [
        {"name": "page_media_view", "period": "day", "values": [
            {"value": 10, "end_time": "2026-11-11T08:00:00+0000"},
            {"value": 5, "end_time": "2026-11-12T08:00:00+0000"}]},
        {"name": "page_follows", "period": "day", "values": [
            {"value": 63000, "end_time": "2026-11-12T08:00:00+0000"}]}]})
    out = call(_srv().get_page_insights)(metrics=["page_media_view", "page_follows", "page_follows_country"],
                                         since="2026-11-10", until="2026-11-11")
    m = out["metrics"]
    assert m["page_media_view"]["total"] == 15
    assert m["page_media_view"]["series"][0] == {"date": "2026-11-10", "value": 10}
    assert m["page_follows"]["latest"] == 63000 and "total" not in m["page_follows"]
    assert list(m["page_follows_country"]["latest"]) == ["PL", "DE"]


def test_page_insights_retired_metric_has_hint(fb_account):
    out = call(_srv().get_page_insights)(metrics=["page_impressions"])
    assert out["error_class"] == "validation" and "retired" in out["error"]


def test_page_insights_range_limit(fb_account):
    out = call(_srv().get_page_insights)(since="2026-01-01", until="2026-11-01")
    assert out["error_class"] == "validation" and "90" in out["error"]


def test_page_insights_reports_unavailable(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/insights$", {"data": []})
    out = call(_srv().get_page_insights)()
    assert "page_media_view" in out["unavailable"]


def test_post_insights(fb_account):
    fb_account.route("GET", rf"^/{POST}/insights$", {"data": [
        {"name": "post_media_view", "period": "lifetime", "values": [{"value": 5603}]},
        {"name": "post_reactions_by_type_total", "period": "lifetime", "values": [{"value": {"like": 3}}]}]})
    out = call(_srv().get_post_insights)(post_id=POST, include_video=True)
    assert out["metrics"]["post_media_view"] == 5603
    assert out["metrics"]["post_reactions_by_type_total"] == {"like": 3}
    assert out["metrics"]["post_video_views"] is None
    assert "post_video_views" in fb_account.calls("GET", r"/insights$")[0].params["metric"]


def test_insights_summary(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", {"followers_count": 63553})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/insights$", lambda r: {"data": [
        {"name": "page_total_media_view_unique", "values": [{"value": 84696}]}]}
        if r.params["metric"] == "page_total_media_view_unique" else {"data": [
        {"name": "page_media_view", "values": [{"value": 1}, {"value": 2}]},
        {"name": "page_views_total", "values": [{"value": 4}]}]})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}/posts$", {"data": [_post(1, 5, 5), _post(2, 1, 0)]})
    out = call(_srv().insights_summary)(period="last_28_days")
    assert out["followers"] == 63553 and out["totals"]["page_media_view"] == 3
    assert out["reach_rolling"] == {"window": "28 days", "value": 84696}
    assert out["posts_published"] == 2 and out["post_engagement_total"] == 11
    assert out["top_posts"][0]["id"] == f"{FB_PAGE_ID}_1"


def test_errors_never_leak_tokens(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", (500, {"error": {"code": 1, "message": "bad EAAfbpagetoken00000000000000000"}}))
    out = call(_srv().get_page)()
    assert out["ok"] is False and "EAAfbpagetoken0" not in str(out)
