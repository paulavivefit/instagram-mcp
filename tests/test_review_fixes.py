"""Stage 9: fixes for the independent review findings (each test reproduces one finding)."""

from __future__ import annotations

import json

import httpx

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, call

SEND = rf"^/{FAKE_PAGE_ID}/messages$"


# 1. Page-token cache must not outlive an account change / revoked token
def test_page_cache_follows_account_token_changes(monkeypatch, dm_account):
    from instagram_mcp import server
    dm_account.route("POST", r"/messages$", {"message_id": "m"})
    call(server.send_message)("921", "one")
    # same label, different IG account + token -> must resolve a new Page
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", "EAAsecondtoken0000000000000000")
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", "17841499999999999")
    dm_account.route("GET", r"^/me/accounts$", {"data": [
        {"id": "200000000000002", "access_token": "EAApageB0000000000000000000",
         "instagram_business_account": {"id": "17841499999999999"}}]})
    call(server.send_message)("921", "two")
    last = dm_account.calls("POST", r"/messages$")[-1]
    assert last.path == "/200000000000002/messages"


def test_page_cache_evicted_on_oauth_error(dm_account):
    from instagram_mcp import server
    dm_account.route("POST", SEND, (400, {"error": {"message": "token expired", "code": 190,
                                                   "type": "OAuthException"}}))
    assert call(server.send_message)("921", "x")["error_class"] == "oauth"
    call(server.send_message)("921", "x")
    assert len(dm_account.calls("GET", r"^/me/accounts$")) == 2


# 2. partial send must say what already went out
def test_partial_send_reports_sent_messages(dm_account):
    from instagram_mcp import server

    def responder(req):
        if "attachment" in req.form["message"]:
            return (400, {"error": {"message": "Invalid image", "code": 100}})
        return {"message_id": "mid.text"}

    dm_account.route("POST", SEND, responder)
    res = call(server.send_message)("921", "Here you go", image_url="https://cdn.example.com/x.jpg")
    assert res["ok"] is False
    assert res["sent_before_error"] == 1 and res["message_ids"] == ["mid.text"]
    assert "do not resend" in res["hint"].lower()


# 3. a publish that times out must hand back the container id
def test_media_publish_timeout_returns_container_id(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C9"})
    account.route("POST", rf"^/{FAKE_IG_ID}/media_publish$", httpx.ReadTimeout("slow"))
    res = call(server.publish_image)("https://cdn.example.com/a.jpg")
    assert res["ok"] is False and res["error_class"] == "timeout"
    assert res["container_id"] == "C9"
    assert "list_media" in res["hint"]
    assert len(account.calls("POST", r"/media_publish$")) == 1


# 4. a carousel video that is still processing: don't suggest publishing the child
def test_carousel_video_timeout_hint(account, monkeypatch):
    from instagram_mcp import server
    monkeypatch.setattr(server, "VIDEO_WAIT_S", 0)
    n = {"i": 0}

    def create(req):
        n["i"] += 1
        return {"id": f"K{n['i']}"}

    account.route("POST", rf"^/{FAKE_IG_ID}/media$", create)
    account.route("GET", r"^/K\d+$", {"status_code": "IN_PROGRESS"})
    res = call(server.publish_carousel)(["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.mp4"])
    assert res["ok"] is False and res["error_class"] == "timeout"
    assert "container_id" not in res
    assert "publish_carousel" in res["hint"] and "publish_container" not in res["hint"]
    assert account.calls("POST", r"/media_publish$") == []


# 5. `until` must not eat the limit
def test_list_media_until_does_not_consume_limit(account):
    from instagram_mcp import server
    from tests.test_pagination import _media, _paged
    account.route("GET", rf"^/{FAKE_IG_ID}/media$", _paged(_media(60)))  # newest 2026-11-30
    res = call(server.list_media)(limit=5, until="2026-11-05")
    assert res["count"] == 5
    assert all(m["timestamp"][:10] <= "2026-11-05" for m in res["media"])


# 6. Meta returning more than asked must not lose items
def test_paginate_keeps_items_when_meta_over_returns(account):
    from instagram_mcp import auth
    items = [{"id": str(i)} for i in range(60)]

    def responder(req):
        start = int(req.params.get("after") or 0)
        chunk = items[start:start + 25]  # ignores ?limit
        out = {"data": chunk}
        if start + 25 < len(items):
            out["paging"] = {"cursors": {"after": str(start + 25)}, "next": "n"}
        return out

    account.route("GET", r"^/X/edge$", responder)
    client, _ = auth.client_for(None)
    got, cursor = client.paginate("X/edge", max_items=10)
    resumed, _ = client.paginate("X/edge", max_items=100, after=cursor)
    ids = [g["id"] for g in got + resumed]
    assert ids == [str(i) for i in range(60)]  # nothing skipped, nothing duplicated


# 7. schema/argument gaps
def test_get_messages_accepts_all(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", r"^/conv1$", {"messages": {"data": [{"id": "1"}]}})
    assert call(server.get_messages)("conv1", limit="all")["ok"] is True


def test_location_id_accepts_number():
    from instagram_mcp import validators as V
    assert V.validate_location_id(110001) == "110001"


def test_tool_schemas_accept_string_limits_and_numeric_location():
    import asyncio

    from instagram_mcp import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    msg_limit = json.dumps(tools["get_messages"].parameters["properties"]["limit"])
    assert "string" in msg_limit
    loc = json.dumps(tools["publish_image"].parameters["properties"]["location_id"])
    assert "integer" in loc and "string" in loc


# extras from the review
def test_igaa_tokens_are_redacted():
    from instagram_mcp import audit
    assert "IGAAabc" not in audit.sanitize_error("bad token IGAAabcdefghijklmnopqrstuvwxyz0123")


def test_long_cursor_accepted():
    from instagram_mcp import validators as V
    assert V.validate_cursor("Q" * 1500)



def test_until_filter_uses_full_pages(account):
    from instagram_mcp import server
    from tests.test_pagination import _media, _paged
    account.route("GET", rf"^/{FAKE_IG_ID}/media$", _paged(_media(320)))  # 2026-11-30 back daily
    res = call(server.list_media)(limit=5, until="2026-06-01")
    assert res["count"] == 5
    calls = account.calls("GET", rf"^/{FAKE_IG_ID}/media$")
    assert len(calls) <= 4 and all(c.params["limit"] == "100" for c in calls)


def test_publish_rejection_has_no_may_be_live_hint(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C9"})
    account.route("POST", rf"^/{FAKE_IG_ID}/media_publish$",
                  (400, {"error": {"message": "(#9) daily posting limit reached", "code": 9}}))
    res = call(server.publish_image)("https://cdn.example.com/a.jpg")
    assert res["ok"] is False and res["container_id"] == "C9"
    assert "MAY already be live" not in res.get("hint", "")
