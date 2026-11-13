"""Stage 13: feedback from real use in Claude Desktop (one continuation token, compact output)."""

from __future__ import annotations

from tests.conftest import FAKE_IG_ID, call
from tests.test_pagination import _media, _paged

MEDIA = rf"^/{FAKE_IG_ID}/media$"



def test_date_filtered_page_continues_with_next_cursor(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(320)))  # daily posts from 2026-11-30 back
    first = call(server.list_media)(limit=5, until="2026-06-01")
    assert first["count"] == 5 and first["next_cursor"] and "next_until" not in first
    second = call(server.list_media)(limit=5, until="2026-06-01", after=first["next_cursor"])
    days = [m["timestamp"][:10] for m in first["media"] + second["media"]]
    assert days == sorted(days, reverse=True) and len(set(days)) == 10  # no gap, no duplicate
    assert days[4] > days[5]


def test_synthetic_cursor_respects_since(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(320)))
    first = call(server.list_media)(limit=3, since="2026-05-25", until="2026-06-01")
    rest = call(server.list_media)(limit=50, since="2026-05-25", until="2026-06-01", after=first["next_cursor"])
    days = [m["timestamp"][:10] for m in first["media"] + rest["media"]]
    assert days[0] == "2026-06-01" and days[-1] == "2026-05-25" and len(days) == 8
    assert rest.get("next_cursor") is None


def test_compact_mode_returns_only_analysis_fields(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, _paged(_media(10)))
    call(server.list_media)(limit=5, compact=True)
    fields = set(account.calls("GET", MEDIA)[0].params["fields"].split(","))
    assert fields == {"id", "timestamp", "media_type", "media_product_type", "like_count",
                      "comments_count", "permalink", "caption"}
    call(server.list_media)(limit=5)
    assert "media_url" in account.calls("GET", MEDIA)[-1].params["fields"]


def test_compact_mode_on_mentions(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}/tags$", {"data": []})
    call(server.get_mentions)(compact=True)
    assert "media_url" not in account.calls("GET", r"/tags$")[0].params["fields"]


def test_synthetic_cursor_format_accepted_and_real_cursor_untouched():
    from instagram_mcp import validators as V
    assert V.validate_cursor("until-1780000000") == "until-1780000000"
    assert V.parse_until_cursor("until-1780000000") == 1780000000
    assert V.parse_until_cursor("QVFIUmFiY2RlZg==") is None


def test_top_posts_uses_compact_fields(account):
    from instagram_mcp import server
    account.route("GET", MEDIA, {"data": []})
    call(server.top_posts)(metric="likes")
    assert "media_url" not in account.calls("GET", MEDIA)[0].params["fields"]


def test_compact_shortens_long_captions(account):
    from instagram_mcp import server
    long_cap = "x" * 500
    account.route("GET", MEDIA, {"data": [{"id": "1", "timestamp": "2026-11-01T10:00:00+0000", "caption": long_cap},
                                          {"id": "2", "timestamp": "2026-10-31T10:00:00+0000", "caption": "short"}]})
    res = call(server.list_media)(limit=5, compact=True)
    a, b = res["media"]
    assert len(a["caption"]) <= 151 and a["caption_truncated"] is True
    assert b["caption"] == "short" and "caption_truncated" not in b
    full = call(server.list_media)(limit=5)
    assert full["media"][0]["caption"] == long_cap  # full mode untouched
