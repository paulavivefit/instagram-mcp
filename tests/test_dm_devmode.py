"""Stage 12: Meta's development-mode conversation timeout (live finding 2026-11-12)."""

from __future__ import annotations

from tests.conftest import FAKE_PAGE_ID, call

CONVS = rf"^/{FAKE_PAGE_ID}/conversations$"
DEV_TIMEOUT = (400, {"error": {"message": "Timeout", "type": "OAuthException", "code": -2,
                               "error_subcode": 2534084, "is_transient": False}})


def test_dev_mode_timeout_is_not_an_oauth_error(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", CONVS, DEV_TIMEOUT)
    res = call(server.list_conversations)()
    assert res["ok"] is False
    assert res["error_class"] == "dev_mode_limit"
    assert "user_id" in res["hint"] and "App Review" in res["hint"]


def test_dev_mode_timeout_keeps_page_cache(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", CONVS, DEV_TIMEOUT)
    call(server.list_conversations)()
    call(server.list_conversations)()
    assert len(dm_account.calls("GET", r"^/me/accounts$")) == 1


def test_list_conversations_with_user_id_looks_up_directly(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", CONVS, {"data": [{"id": "conv9", "updated_time": "t"}]})
    dm_account.route("GET", r"^/conv9$", {"participants": {"data": [{"username": "bnancore", "id": "921"}]}})
    res = call(server.list_conversations)(user_id="921")
    assert res["ok"] is True and res["conversations"][0]["id"] == "conv9"
    params = dm_account.calls("GET", CONVS)[0].params
    assert params["user_id"] == "921" and params["platform"] == "instagram"


def test_list_conversations_user_id_validated(dm_account):
    from instagram_mcp import server
    res = call(server.list_conversations)(user_id="../x")
    assert res["ok"] is False and res["error_class"] == "validation"


def test_no_role_recipient_gets_hint_in_any_language(dm_account):
    from instagram_mcp import server
    err = (403, {"error": {"message": "(#200) Reklamy nie posiadają zaawansowanego dostępu do uprawnienia "
                                      "instagram_manage_messages, a odbiorca nie ma przypisanej roli w aplikacji.",
                           "type": "OAuthException", "code": 200}})
    dm_account.route("POST", rf"^/{FAKE_PAGE_ID}/messages$", err)
    for res in (call(server.send_message)("921", "hi"), call(server.mark_seen)("921")):
        assert res["ok"] is False and res["error_class"] == "permission"
        assert "Tester" in res["hint"] and "App Review" in res["hint"]
