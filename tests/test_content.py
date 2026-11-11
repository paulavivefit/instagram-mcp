"""Stage 5: comment replies, carousel children, stories (shapes verified live 2026-11-11)."""

from __future__ import annotations

import json

from tests.conftest import FAKE_IG_ID, call


def test_get_comments_include_replies_expands_field(account):
    from instagram_mcp import server
    account.route("GET", r"^/555/comments$", {"data": [
        {"id": "c1", "text": "hi", "replies": {"data": [{"id": "r1", "text": "yo", "username": "paula"}]}},
        {"id": "c2", "text": "no replies"}]})
    res = call(server.get_comments)("555", include_replies=True)
    assert res["ok"] is True, res
    fields = account.calls("GET", r"^/555/comments$")[0].params["fields"]
    assert "replies{" in fields
    assert res["comments"][0]["replies"]["data"][0]["id"] == "r1"
    assert res["reply_count"] == 1


def test_get_comments_without_replies_keeps_plain_fields(account):
    from instagram_mcp import server
    account.route("GET", r"^/555/comments$", {"data": []})
    call(server.get_comments)("555")
    assert "replies" not in account.calls("GET", r"^/555/comments$")[0].params["fields"]


def test_get_comment_replies_pages(account):
    from instagram_mcp import server

    def responder(req):
        start = int(req.params.get("after") or 0)
        out = {"data": [{"id": f"r{i}"} for i in range(start, min(start + int(req.params["limit"]), 130))]}
        if start + int(req.params["limit"]) < 130:
            out["paging"] = {"cursors": {"after": str(start + int(req.params["limit"]))}, "next": "n"}
        return out

    account.route("GET", r"^/c1/replies$", responder)
    res = call(server.get_comment_replies)("c1", limit="all")
    assert res["ok"] is True, res
    assert res["count"] == 130


def test_get_media_includes_carousel_children(account):
    from instagram_mcp import server
    account.route("GET", r"^/777$", {"id": "777", "media_type": "CAROUSEL_ALBUM",
                                     "children": {"data": [{"id": "k1", "media_type": "IMAGE"},
                                                           {"id": "k2", "media_type": "VIDEO"}]}})
    res = call(server.get_media)("777")
    assert res["ok"] is True, res
    assert "children{" in account.calls("GET", r"^/777$")[0].params["fields"]
    assert [c["id"] for c in res["media"]["children"]["data"]] == ["k1", "k2"]


def test_list_stories_empty(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}/stories$", {"data": []})
    res = call(server.list_stories)()
    assert res == {**res, "ok": True, "count": 0, "stories": []}


def test_list_stories_with_insights_uses_story_metrics(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}/stories$", {"data": [
        {"id": "s1", "media_type": "IMAGE", "media_product_type": "STORY"}]})

    def batch(req):
        items = json.loads(req.form["batch"])
        assert "replies" in items[0]["relative_url"] and "likes" not in items[0]["relative_url"]
        return [{"code": 200, "body": json.dumps({"data": [{"name": "reach", "values": [{"value": 9}]}]})}]

    account.route("POST", r"^/?$", batch)
    res = call(server.list_stories)(include_insights=True)
    assert res["ok"] is True, res
    assert res["stories"][0]["metrics"]["reach"] == 9
