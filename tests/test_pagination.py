"""Stage 2: limits, "all", cursors and date filters."""

from __future__ import annotations

import pytest

from tests.conftest import FAKE_IG_ID, call

MEDIA = rf"^/{FAKE_IG_ID}/media$"


def _paged(items, page_key="after"):
    """Responder that serves `items` in pages honouring ?limit and ?after (index cursor)."""
    def responder(req):
        start = int(req.params.get(page_key) or 0)
        size = int(req.params.get("limit", 25))
        chunk = items[start:start + size]
        out = {"data": chunk}
        nxt = start + size
        if nxt < len(items):
            out["paging"] = {"cursors": {"after": str(nxt)}, "next": "https://graph.facebook.com/n"}
        return out
    return responder


def _media(n, *, start_day=30):
    # newest first, one post per day going back from 2026-11-{start_day}
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 11, start_day, 12, tzinfo=timezone.utc)
    return [{"id": str(1000 + i),
             "timestamp": (base - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
            for i in range(n)]


# -------------------------------- validators -------------------------------- #

def test_validate_limit_accepts_all_and_numeric_strings():
    from instagram_mcp import validators as V
    assert V.validate_limit("all", ceiling=100, all_ceiling=10000) == 10000
    assert V.validate_limit("ALL", ceiling=500) == 500
    assert V.validate_limit("40") == 40
    assert V.validate_limit(5000, ceiling=10000) == 5000
    for bad in ("lots", 0, -1, 10001, True, 2.5):
        with pytest.raises(V.ValidationError):
            V.validate_limit(bad, ceiling=10000)


# --------------------------------- client ----------------------------------- #

def test_paginate_uses_page_size_100_and_returns_cursor(account):
    from instagram_mcp import auth
    account.route("GET", MEDIA, _paged(_media(250)))
    client, _ = auth.client_for(None)
    items, cursor = client.paginate(f"{FAKE_IG_ID}/media", max_items=150)
    assert len(items) == 150
    assert [c.params["limit"] for c in account.calls("GET", MEDIA)] == ["100", "50"]
    assert cursor == "150"


def test_paginate_resumes_from_cursor_and_ends_with_none(account):
    from instagram_mcp import auth
    account.route("GET", MEDIA, _paged(_media(120)))
    client, _ = auth.client_for(None)
    items, cursor = client.paginate(f"{FAKE_IG_ID}/media", max_items=500, after="100")
    assert [i["id"] for i in items] == [str(1000 + i) for i in range(100, 120)]
    assert cursor is None


def test_get_paginated_still_works(account):
    from instagram_mcp import auth
    account.route("GET", MEDIA, _paged(_media(30)))
    client, _ = auth.client_for(None)
    assert len(client.get_paginated(f"{FAKE_IG_ID}/media", limit=10, max_items=25)) == 25


# ---------------------------------- tools ----------------------------------- #

def test_list_media_all_walks_every_page(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(345)))
    res = call(server.list_media)(limit="all")
    assert res["ok"] is True, res
    assert res["count"] == 345
    assert res.get("next_cursor") is None and not res.get("truncated")


def test_list_media_more_than_100_returns_next_cursor(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(345)))
    res = call(server.list_media)(limit=200)
    assert res["count"] == 200 and res["next_cursor"] == "200" and res["truncated"] is True
    res2 = call(server.list_media)(limit=200, after=res["next_cursor"])
    assert res2["count"] == 145 and res2.get("next_cursor") is None


def test_list_media_since_until_filters_and_stops_early(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(300)))
    res = call(server.list_media)(limit="all", since="2026-11-01", until="2026-11-10")
    assert res["ok"] is True, res
    days = sorted(m["timestamp"][:10] for m in res["media"])
    assert days[0] == "2026-11-01" and days[-1] == "2026-11-10" and len(days) == 10
    # stopped after reaching posts older than `since` -> did not walk all 3 pages
    assert len(account.calls("GET", MEDIA)) == 1


def test_list_media_rejects_bad_dates(account):
    from instagram_mcp import server
    res = call(server.list_media)(since="yesterday")
    assert res["ok"] is False and res["error_class"] == "validation"
    res = call(server.list_media)(since="2026-11-10", until="2026-11-01")
    assert res["ok"] is False and res["error_class"] == "validation"


def test_get_comments_all(account):
    from instagram_mcp import server
    account.route("GET", r"^/555/comments$", _paged([{"id": str(i)} for i in range(230)]))
    res = call(server.get_comments)("555", limit="all")
    assert res["count"] == 230


def test_get_mentions_and_hashtag_media_accept_all_with_ceiling(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}/tags$", _paged([{"id": str(i)} for i in range(1500)]))
    res = call(server.get_mentions)(limit="all")
    assert res["count"] == 1000 and res["truncated"] is True and res["next_cursor"]
    account.route("GET", r"^/777/top_media$", _paged([{"id": str(i)} for i in range(120)]))
    res = call(server.get_hashtag_media)("777", limit="all")
    assert res["count"] == 120


def test_business_discovery_pages_nested_media(account):
    from instagram_mcp import server
    posts = [{"id": str(i)} for i in range(130)]

    def responder(req):
        f = req.params["fields"]
        import re
        lim = int(re.search(r"\.limit\((\d+)\)", f).group(1))
        m = re.search(r"\.after\(([^)]+)\)", f)
        start = int(m.group(1)) if m else 0
        chunk = posts[start:start + lim]
        media = {"data": chunk}
        if start + lim < len(posts):
            media["paging"] = {"cursors": {"after": str(start + lim)}}
        return {"business_discovery": {"username": "rival", "followers_count": 5, "media": media}}

    account.route("GET", rf"^/{FAKE_IG_ID}$", responder)
    res = call(server.business_discovery)("rival", with_media=True, media_limit="all")
    assert res["ok"] is True, res
    bd = res["business_discovery"]
    assert len(bd["media"]["data"]) == 130 and bd["username"] == "rival"


def test_get_messages_respects_limit_even_if_meta_returns_more(monkeypatch, dm_account):
    from instagram_mcp import server
    dm_account.route("GET", r"^/conv1$", {"messages": {"data": [{"id": str(i)} for i in range(5)]}})
    res = call(server.get_messages)("conv1", limit=2)
    assert len(res["messages"]) == 2
