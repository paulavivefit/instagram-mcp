"""Stage A: correctness findings from the independent review (each test reproduces one)."""

from __future__ import annotations

import json
import math

import httpx
import pytest

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, call


@pytest.fixture
def offline_auth(monkeypatch, tmp_path):
    """auth.py with the file backend (no keychain) in a temp dir."""
    from instagram_mcp import auth
    monkeypatch.setattr(auth, "_keychain_available", lambda: False)
    monkeypatch.setenv("INSTAGRAM_MCP_TOKENS_PATH", str(tmp_path / "tokens.json"))
    return auth


# A1: paginate must never exceed max_items when stop fires while skip is active
def test_paginate_trims_when_stop_fires_with_skip(account):
    from instagram_mcp import auth
    items = [{"id": str(i), "n": i} for i in range(40)]
    account.route("GET", r"^/X/e$", {"data": items})
    client, _ = auth.client_for(None)
    got, cursor = client.paginate("X/e", max_items=5, skip=lambda it: it["n"] < 10, stop=lambda it: it["n"] >= 30)
    assert [g["n"] for g in got] == [10, 11, 12, 13, 14]
    assert client.last_cut is True and cursor is None


# A2: adding/removing accounts must never silently move the default
def test_add_account_does_not_take_over_env_default(offline_auth, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", "EAAenvtoken00000000000000000")
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", "17841400000000001")
    offline_auth.add_account("client-a", "EAAclienttoken000000000000000", "17841400000000002")
    assert offline_auth.resolve(None).label == "env"


def test_first_account_becomes_default_without_env(offline_auth):
    offline_auth.add_account("main", "EAAmaintoken0000000000000000", "17841400000000003")
    assert offline_auth.resolve(None).label == "main"


def test_removing_default_does_not_pick_another_when_several_remain(offline_auth):
    for i, label in enumerate(("a", "b", "c")):
        offline_auth.add_account(label, f"EAAtok{i}000000000000000000000", f"1784140000000000{i}", make_default=(label == "a"))
    offline_auth.remove_account("a")
    with pytest.raises(offline_auth.AuthError):
        offline_auth.resolve(None)


def test_removing_default_keeps_sole_remaining(offline_auth):
    offline_auth.add_account("a", "EAAtoka000000000000000000000", "17841400000000001", make_default=True)
    offline_auth.add_account("b", "EAAtokb000000000000000000000", "17841400000000002")
    offline_auth.remove_account("a")
    assert offline_auth.resolve(None).label == "b"


def test_reserved_label_env_rejected(offline_auth):
    with pytest.raises(offline_auth.AuthError):
        offline_auth.add_account("env", "EAAtok00000000000000000000000", "17841400000000001")


# A3: Meta puts type OAuthException on many non-token errors
@pytest.mark.parametrize("code, subcode, expected", [
    (9007, 2207027, "not_ready"),
    (36003, None, "upstream_error"),
    (190, None, "oauth"),
    (102, None, "oauth"),
])
def test_oauthexception_type_alone_is_not_oauth(code, subcode, expected):
    from instagram_mcp import graph_client
    err = graph_client._map_graph_error({"error": {"message": "x", "type": "OAuthException", "code": code,
                                                   "error_subcode": subcode}}, 400)
    assert err.error_class == expected


# A4: publish_container / polling handle PUBLISHED and EXPIRED, keep container_id on poll errors
def test_publish_container_already_published(account):
    from instagram_mcp import server
    account.route("GET", r"^/C1$", {"status_code": "PUBLISHED"})
    res = call(server.publish_container)("C1")
    assert res["ok"] is True and res["already_published"] is True
    assert account.calls("POST", r"/media_publish$") == []


def test_publish_container_expired(account):
    from instagram_mcp import server
    account.route("GET", r"^/C1$", {"status_code": "EXPIRED"})
    res = call(server.publish_container)("C1")
    assert res["ok"] is False and res["error_class"] == "expired" and "again" in res["hint"]


def test_wait_stops_on_expired_and_keeps_id_on_poll_error(account, monkeypatch):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C7"})
    account.route("GET", r"^/C7$", {"status_code": "EXPIRED"})
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", prepare_only=True)
    assert res["ok"] is False and res["error_class"] == "expired"
    assert len(account.calls("GET", r"^/C7$")) == 1
    account.route("GET", r"^/C7$", (500, {"error": {"message": "boom", "code": 2}}))
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", prepare_only=True)
    assert res["ok"] is False and res["container_id"] == "C7"


# A5: wrapping errors keeps Meta's details and its specific hint
def test_publish_error_keeps_code_subcode_and_specific_hint(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C9"})
    account.route("POST", rf"^/{FAKE_IG_ID}/media_publish$", (400, {"error": {
        "message": "Invalid user id", "code": 110, "error_subcode": 2207018}}))
    res = call(server.publish_image)("https://cdn.example.com/a.jpg")
    assert res["container_id"] == "C9"
    assert res["hint"].startswith("Instagram could not find that user")
    assert res.get("code") == 110 and res.get("subcode") == 2207018


# A6: batch replies shorter than the request, and rows without a name
def test_batch_missing_replies_and_nameless_rows(account, monkeypatch):
    from instagram_mcp import auth, server
    posts = [{"id": "P1", "media_product_type": "FEED"}, {"id": "P2", "media_product_type": "FEED"}]
    account.route("POST", r"^/?$", [{"code": 200, "body": json.dumps({"data": [{"values": [{"value": 1}]},
                                                                                {"name": "reach", "values": [{"value": 5}]}]})}])
    client, _ = auth.client_for(None)
    errors = server._batch_media_insights(client, posts)
    assert posts[0]["metrics"] == {"reach": 5}
    assert [e["id"] for e in errors] == ["P2"]


# A7: a failed window makes change_pct None (not a fake drop)
def test_summary_failed_window_marks_incomplete(account, monkeypatch):
    from datetime import datetime, timezone

    from instagram_mcp import server
    monkeypatch.setattr(server, "_utcnow", lambda: datetime(2026, 11, 13, 12, tzinfo=timezone.utc))
    sinces: list[str] = []

    def responder(req):
        if req.params.get("metric_type") == "total_value":
            if req.params["since"] not in sinces:
                sinces.append(req.params["since"])
            if sinces.index(req.params["since"]) == 1:  # one window fails every time (even on retry)
                return (400, {"error": {"message": "(#100) unsupported", "code": 100}})
            return {"data": [{"name": m, "total_value": {"value": 100}} for m in req.params["metric"].split(",")]}
        return {"data": []}

    account.route("GET", rf"^/{FAKE_IG_ID}/insights$", responder)
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"followers_count": 1})
    res = call(server.insights_summary)(period="last_90_days")
    assert res["ok"] is True
    assert all(v is None for v in res["change_pct"].values())


# A8: date input
def test_compact_date_rejected_everywhere(account):
    from instagram_mcp import server
    for tool, kw in ((server.list_media, {"since": "20261001"}), (server.get_account_insights, {"since": "20261001"})):
        res = call(tool)(**kw)
        assert res["ok"] is False and res["error_class"] == "validation"


def test_blank_metrics_rejected(account):
    from instagram_mcp import server
    assert call(server.get_account_insights)(metrics=",")["error_class"] == "validation"


# A9: _request robustness
@pytest.mark.parametrize("value", ["-5", "nan", "inf", "soon"])
def test_bad_retry_after_ignored(account, monkeypatch, value):
    from instagram_mcp import graph_client
    assert graph_client._retry_after({"Retry-After": value}) is None


def test_error_null_body_handled():
    from instagram_mcp import graph_client
    err = graph_client._map_graph_error({"error": None}, 500)
    assert isinstance(err, graph_client.GraphAPIError)


def test_non_json_2xx_on_post_is_not_success(account, monkeypatch):
    from instagram_mcp import auth, graph_client
    real = graph_client.DEFAULT_TRANSPORT

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            if request.method == "POST":
                return httpx.Response(200, content=b"<html>oops</html>")
            return real.handle_request(request)

    monkeypatch.setattr(graph_client, "DEFAULT_TRANSPORT", T())
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.post("1/comments", message="x")
    assert exc.value.error_class == "upstream_error"


# A10: small fixes
def test_list_conversations_later_page_error_keeps_cursor(dm_account):
    from instagram_mcp import server

    def conv(req):
        if "after" in req.params:
            return (400, {"error": {"message": "Application request limit reached", "code": 4}})
        return {"data": [{"id": "c1", "updated_time": "t"}],
                "paging": {"cursors": {"after": "CUR1"}, "next": "https://graph.facebook.com/n"}}

    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", conv)
    dm_account.route("GET", r"^/c1$", {"participants": {"data": []}})
    res = call(server.list_conversations)(limit=5)
    assert res["ok"] is True and res["next_cursor"] == "CUR1"
    assert "development mode" not in res.get("note", "")


def test_business_discovery_truncated_from_media_count(account):
    """Live: nested media paging only has cursors (no 'next') even when more posts exist,
    so truncation is judged against the profile's media_count."""
    from instagram_mcp import server
    page = {"data": [{"id": str(i)} for i in range(12)], "paging": {"cursors": {"after": "Z"}}}
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"business_discovery": {"username": "x", "media_count": 12, "media": page}})
    assert not call(server.business_discovery)("x", with_media=True, media_limit=12).get("truncated")
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"business_discovery": {"username": "x", "media_count": 40, "media": page}})
    assert call(server.business_discovery)("x", with_media=True, media_limit=12).get("truncated") is True


def test_no_page_linked_is_setup_error(monkeypatch, account):
    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    account.route("GET", r"^/me/accounts$", {"data": []})
    from instagram_mcp import server
    assert call(server.list_conversations)()["error_class"] == "setup"


def test_nan_is_nan():  # guard for the parametrized NaN case above
    assert math.isnan(float("nan"))
