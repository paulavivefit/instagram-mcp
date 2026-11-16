"""The Page lock: only allowed Pages are ever used; forbidden Page tokens never leave me/accounts."""

from __future__ import annotations

from tests.conftest import FAKE_IG_ID, FB_PAGE_ID, FB_PAGE_TOKEN, OTHER_PAGE_ID, OTHER_PAGE_TOKEN, call


def _server():
    from facebook_mcp import server
    return server


def test_list_pages_shows_only_allowed(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", {"followers_count": 10, "fan_count": 10, "link": "x"})
    out = call(_server().list_pages)()
    assert out["ok"] and out["count"] == 1
    assert out["pages"][0]["page_id"] == FB_PAGE_ID
    assert "access_token" not in str(out)


def test_forbidden_page_id_refused_without_any_call_on_it(fb_account):
    out = call(_server().get_page)(page_id=OTHER_PAGE_ID)
    assert out["ok"] is False and out["error_class"] == "forbidden_page"
    assert not [r for r in fb_account.requests if OTHER_PAGE_ID in r.path]
    assert all(r.token != OTHER_PAGE_TOKEN for r in fb_account.requests)


def test_page_requests_use_page_token(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", {"id": FB_PAGE_ID, "name": "Trening Fitness"})
    out = call(_server().get_page)()
    assert out["ok"] and out["page_id"] == FB_PAGE_ID
    assert fb_account.calls("GET", rf"^/{FB_PAGE_ID}$")[0].token == FB_PAGE_TOKEN


def test_default_without_env_is_page_linked_to_instagram(monkeypatch, fb_account):
    monkeypatch.delenv("FACEBOOK_MCP_ALLOWED_PAGE_IDS")
    from facebook_mcp import pages
    allowed = pages.allowed_pages()
    assert list(allowed) == [FB_PAGE_ID]
    assert allowed[FB_PAGE_ID].ig_user_id == FAKE_IG_ID


def test_no_allowed_page_is_setup_error(monkeypatch, fb_account):
    monkeypatch.setenv("FACEBOOK_MCP_ALLOWED_PAGE_IDS", "111")
    fb_account.route("GET", r"^/me$", {"id": "1", "name": "x"})
    fb_account.route("GET", r"^/me/permissions$", {"data": []})
    out = call(_server().healthcheck)()
    assert out["ok"] is False and out["error_class"] == "setup"


def test_bad_env_value_is_setup_error(monkeypatch, fb_account):
    monkeypatch.setenv("FACEBOOK_MCP_ALLOWED_PAGE_IDS", "abc,123")
    out = call(_server().get_page)()
    assert out["error_class"] == "setup"


def test_non_numeric_page_id_is_validation(fb_account):
    out = call(_server().get_page)(page_id="../me")
    assert out["error_class"] == "validation"


def test_foreign_post_id_refused(fb_account):
    out = call(_server().get_post)(post_id=f"{OTHER_PAGE_ID}_123")
    assert out["error_class"] == "forbidden_page"
    out = call(_server().delete_post)(post_id=f"{OTHER_PAGE_ID}_123")
    assert out["error_class"] == "forbidden_page"
    assert not fb_account.calls("DELETE", r".")


def test_bare_object_id_checked_via_from(fb_account):
    fb_account.route("GET", r"^/777$", {"id": "777", "from": {"id": OTHER_PAGE_ID}})
    out = call(_server().delete_post)(post_id="777")
    assert out["error_class"] == "forbidden_page"
    assert not fb_account.calls("DELETE", r".")


def test_bare_object_id_owned_is_accepted(fb_account):
    fb_account.route("GET", r"^/778$", {"id": "778", "from": {"id": FB_PAGE_ID}})
    fb_account.route("DELETE", r"^/778$", {"success": True})
    out = call(_server().delete_post)(post_id="778")
    assert out["ok"] and out["deleted"]


def test_foreign_comment_refused(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_555$", (400, {"error": {"code": 100, "message": "nope"}}))
    fb_account.route("GET", r"^/555$", {"id": "555", "from": {"id": OTHER_PAGE_ID}})
    out = call(_server().hide_comment)(comment_id="555_1")
    assert out["error_class"] == "forbidden_page"
    assert not fb_account.calls("POST", r".")


def test_own_comment_accepted_via_post(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_555$", {"id": f"{FB_PAGE_ID}_555"})
    fb_account.route("POST", r"^/555_1$", {"success": True})
    out = call(_server().hide_comment)(comment_id="555_1")
    assert out["ok"] and out["hidden"] is True
    assert fb_account.calls("POST", r"^/555_1$")[0].form["is_hidden"] == "true"


def test_unknown_bare_object_is_not_found(fb_account):
    fb_account.route("GET", r"^/999$", (400, {"error": {"code": 100, "message": "Unsupported get request"}}))
    out = call(_server().get_post)(post_id="999")
    assert out["error_class"] == "not_found"


def test_malformed_ids_are_validation(fb_account):
    srv = _server()
    assert call(srv.get_post)(post_id="1/2")["error_class"] == "validation"
    assert call(srv.hide_comment)(comment_id="12345")["error_class"] == "validation"
    assert call(srv.hide_comment)(comment_id="a_b")["error_class"] == "validation"


def test_page_list_is_cached(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", {"id": FB_PAGE_ID})
    call(_server().get_page)()
    call(_server().get_page)()
    assert len(fb_account.calls("GET", r"^/me/accounts$")) == 1


def test_oauth_error_clears_page_cache(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}$", (400, {"error": {"code": 190, "message": "expired"}}))
    out = call(_server().get_page)()
    assert out["error_class"] == "oauth"
    from facebook_mcp import pages
    assert pages._CACHE == {}
