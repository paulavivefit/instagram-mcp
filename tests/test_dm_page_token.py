"""Stage 0: pin the DM behaviour (Page token, fail-soft paging, long conversation ids)."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import FAKE_PAGE_ID, FAKE_PAGE_TOKEN, FAKE_TOKEN, call

CONV_ID = "aWdfZAG06MTpJR01lc3NhZA2VUaHJlYWQ6MTc4NDE0MDE1NTU3MDQxNDk6MzQwMjgy=="


def test_fake_graph_basic_roundtrip(account):
    from instagram_mcp import server
    account.route("GET", r"^/17841400000000000$", {"id": "17841400000000000", "username": "demo"})
    res = call(server.get_profile)()
    assert res["ok"] is True
    assert res["profile"]["username"] == "demo"
    assert account.requests[0].token == FAKE_TOKEN


def test_list_conversations_uses_page_token_on_page_node(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$",
                     {"data": [{"id": CONV_ID, "updated_time": "2026-11-11T18:21:43+0000"}]})
    dm_account.route("GET", rf"^/{CONV_ID}$",
                     {"participants": {"data": [{"username": "biz", "id": "1"},
                                                 {"username": "customer", "id": "921"}]}})
    res = call(server.list_conversations)(limit=1)
    assert res["ok"] is True, res
    assert res["conversations"][0]["participants"][1]["username"] == "customer"
    conv_calls = dm_account.calls("GET", r"/conversations$")
    assert conv_calls and all(c.token == FAKE_PAGE_TOKEN for c in conv_calls)
    assert conv_calls[0].params["limit"] == "1"
    assert conv_calls[0].params["platform"] == "instagram"


def test_page_token_lookup_is_cached(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", {"data": []})
    call(server.list_conversations)(limit=1)
    call(server.list_conversations)(limit=1)
    assert len(dm_account.calls("GET", r"^/me/accounts$")) == 1


def test_list_conversations_fail_soft_when_later_page_hangs(dm_account):
    from instagram_mcp import server

    def conversations(req):
        if "after" in req.params:
            return httpx.ReadTimeout("slow meta")
        return {"data": [{"id": CONV_ID, "updated_time": "t"}],
                "paging": {"cursors": {"after": "CUR1"}, "next": "https://graph.facebook.com/next"}}

    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", conversations)
    dm_account.route("GET", rf"^/{CONV_ID}$", {"participants": {"data": []}})
    res = call(server.list_conversations)(limit=5)
    assert res["ok"] is True, res
    assert res["count"] == 1
    assert "stopped early" in res["note"]


def test_get_messages_accepts_long_base64_conversation_id(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{CONV_ID}$", {"messages": {"data": [
        {"id": "m1", "created_time": "t", "from": {"username": "customer"}, "message": "hi"}]}})
    res = call(server.get_messages)(CONV_ID, limit=3)
    assert res["ok"] is True, res
    assert res["messages"][0]["message"] == "hi"
    assert dm_account.calls("GET", rf"^/{CONV_ID}$")[0].token == FAKE_PAGE_TOKEN


@pytest.mark.parametrize("bad", ["abc/def", "abc?x=1", "a&b", "", "x" * 300])
def test_get_messages_rejects_unsafe_conversation_id(dm_account, bad):
    from instagram_mcp import server
    res = call(server.get_messages)(bad)
    assert res["ok"] is False
    assert res["error_class"] == "validation"
    assert dm_account.calls("GET", r"^/(?!me/accounts)") == []


def test_send_message_posts_to_page_messages(dm_account):
    from instagram_mcp import server
    dm_account.route("POST", rf"^/{FAKE_PAGE_ID}/messages$", {"recipient_id": "921", "message_id": "mid"})
    res = call(server.send_message)("921", "Hello!")
    assert res["ok"] is True, res
    sent = dm_account.calls("POST", rf"^/{FAKE_PAGE_ID}/messages$")[0]
    assert sent.token == FAKE_PAGE_TOKEN
    assert json.loads(sent.form["recipient"]) == {"id": "921"}
    assert json.loads(sent.form["message"]) == {"text": "Hello!"}


def test_dm_errors_when_no_page_linked(monkeypatch, account):
    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    account.route("GET", r"^/me/accounts$", {"data": []})
    from instagram_mcp import server
    res = call(server.list_conversations)()
    assert res["ok"] is False
    assert "Page" in res["error"]
