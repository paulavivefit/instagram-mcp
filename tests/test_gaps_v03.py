"""Stage D: tests for code paths no test reached (review finding B + coverage report)."""

from __future__ import annotations

import json
import os
import subprocess

import httpx
import pytest

from tests.conftest import FAKE_IG_ID, FAKE_PAGE_ID, FAKE_PAGE_TOKEN, FAKE_TOKEN, call

SECRET = "EAAsupersecrettoken1234567890abcdef"


@pytest.fixture
def offline(monkeypatch):
    from instagram_mcp import auth
    monkeypatch.setattr(auth, "_keychain_available", lambda: False)
    return auth


# ------------------------------- auth.py ------------------------------------ #

def test_keychain_get_and_delete_commands(monkeypatch):
    from instagram_mcp import auth
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "find-generic-password" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"{SECRET}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(auth.subprocess, "run", fake_run)
    assert auth._keychain_get("acct") == SECRET
    auth._keychain_delete("acct")
    assert calls[1][:2] == ["security", "delete-generic-password"]


def test_keychain_get_missing_returns_none(monkeypatch):
    from instagram_mcp import auth
    monkeypatch.setattr(auth.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 44, "", "nf"))
    assert auth._keychain_get("acct") is None


@pytest.mark.parametrize("bad", ['tok"en', "tok\nen", "tok\\en"])
def test_keychain_rejects_unsafe_characters(monkeypatch, bad):
    from instagram_mcp import auth
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(auth.AuthError):
        auth._keychain_set("acct", bad)


def test_keychain_missing_binary(monkeypatch):
    from instagram_mcp import auth

    def raise_oserror(*a, **k):
        raise OSError("no security")

    monkeypatch.setattr(auth.subprocess, "run", raise_oserror)
    with pytest.raises(auth.AuthError, match="unavailable"):
        auth._keychain_set("acct", SECRET)


def test_keychain_backend_used_when_available(monkeypatch):
    from instagram_mcp import auth
    store = {}
    monkeypatch.setattr(auth, "_keychain_available", lambda: True)
    monkeypatch.setattr(auth, "_keychain_set", lambda a, s: store.__setitem__(a, s))
    monkeypatch.setattr(auth, "_keychain_get", lambda a: store.get(a))
    monkeypatch.setattr(auth, "_keychain_delete", lambda a: store.pop(a, None))
    auth.add_account("main", SECRET, "17841400000000001", app_secret="0123456789abcdef0123456789abcdef")
    assert store == {"main": SECRET, "main::secret": "0123456789abcdef0123456789abcdef"}
    assert auth.resolve("main").app_secret == "0123456789abcdef0123456789abcdef"
    auth.remove_account("main")
    assert store == {}
    assert auth.secret_backend() == "macos-keychain"


def test_corrupt_files_treated_as_empty(offline):
    offline.ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    offline.ACCOUNTS_PATH.write_text("{not json")
    offline.TOKENS_FALLBACK_PATH.write_text("{not json")
    assert offline.list_accounts() == []
    assert offline._fallback_load() == {}


def test_set_default_and_unknown(offline):
    offline.add_account("a", SECRET, "17841400000000001")
    offline.add_account("b", SECRET, "17841400000000002")
    assert offline.set_default("b") == {"default": "b"}
    assert offline.resolve(None).label == "b"
    with pytest.raises(offline.AuthError):
        offline.set_default("zzz")


def test_list_accounts_includes_env(offline, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", FAKE_IG_ID)
    labels = [a["label"] for a in offline.list_accounts()]
    assert "env" in labels
    assert all(FAKE_TOKEN not in json.dumps(a) for a in offline.list_accounts())


def test_env_account_errors(offline, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", FAKE_TOKEN)
    with pytest.raises(offline.AuthError, match="IG_USER_ID"):
        offline.resolve("env")
    monkeypatch.delenv("INSTAGRAM_MCP_ACCESS_TOKEN")
    with pytest.raises(offline.AuthError):
        offline.resolve("env")


def test_stored_account_without_token(offline):
    offline.add_account("a", SECRET, "17841400000000001")
    offline._secret_delete("a")
    offline.clear_cache()
    with pytest.raises(offline.AuthError, match="no stored token"):
        offline.resolve("a")


def test_non_meta_base_host_rejected(offline):
    with pytest.raises(offline.AuthError, match="base_host"):
        offline.add_account("a", SECRET, "17841400000000001", base_host="evil.example.com")


# ------------------------------- audit.py ----------------------------------- #

@pytest.mark.parametrize("exc, expected", [
    (TimeoutError(), "timeout"), (httpx.ConnectError("x"), "network"), (PermissionError(), "filesystem"),
    (RuntimeError("auth failed"), "auth"), (RuntimeError("boom"), "internal_error"), (KeyError("k"), "upstream_error"),
])
def test_classify_error(exc, expected):
    from instagram_mcp import audit
    assert audit.classify_error(exc) == expected


def test_sanitize_payload_nested_and_non_string():
    from instagram_mcp import audit
    out = audit.sanitize_payload({"a": [f"token {SECRET}", {"b": SECRET}], "n": 5, "o": object})
    assert SECRET not in json.dumps(out, default=str) and out["n"] == 5


def test_tokens_never_reach_output_or_audit(account, monkeypatch):
    """A token inside a Meta error message, an httpx error and extras must be scrubbed."""
    from instagram_mcp import graph_client, server
    account.route("GET", rf"^/{FAKE_IG_ID}$", (400, {"error": {"message": f"bad token {SECRET}", "code": 100}}))
    res = call(server.get_profile)()
    with open(os.environ["INSTAGRAM_MCP_AUDIT_PATH"]) as fh:
        audit_text = fh.read()
    assert SECRET not in json.dumps(res) and SECRET not in audit_text

    def boom():
        raise graph_client.GraphAPIError(f"failed for {SECRET}", extra={"hint": f"x {SECRET}"})

    assert SECRET not in json.dumps(server._guard("t", {"token": SECRET}, boom))


def test_audit_extra_and_unwritable_path(monkeypatch, tmp_path):
    from instagram_mcp import audit
    path = tmp_path / "a.jsonl"
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(path))
    audit.record("t", execution_time_ms=1, io={"input": {}, "output": {}}, extra={"k": SECRET})
    assert SECRET not in path.read_text() and '"extra"' in path.read_text()
    monkeypatch.setenv("INSTAGRAM_MCP_AUDIT_PATH", str(tmp_path / "dir_as_file"))
    (tmp_path / "dir_as_file").mkdir()
    audit.record("t", execution_time_ms=1, io={"input": {}, "output": {}})  # must not raise


def test_unused_timed_helper_removed():
    from instagram_mcp import audit
    assert not hasattr(audit, "timed")


# ---------------------------- graph_client.py ------------------------------- #

def test_post_retried_on_connect_error(account, monkeypatch):
    from instagram_mcp import auth, graph_client
    monkeypatch.setattr(graph_client, "_sleep", lambda s: None)
    seq = [httpx.ConnectError("refused"), {"id": "c1"}]
    account.route("POST", r"^/1/replies$", lambda req: seq.pop(0))
    client, _ = auth.client_for(None)
    assert client.post("1/replies", message="x")["id"] == "c1"


def test_get_timeout_exhausts_attempts(account, monkeypatch):
    from instagram_mcp import auth, graph_client
    monkeypatch.setattr(graph_client, "_sleep", lambda s: None)
    account.route("GET", r"^/X$", httpx.ReadTimeout("slow"))
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.get("X")
    assert exc.value.error_class == "timeout" and len(account.calls("GET", r"^/X$")) == graph_client.MAX_ATTEMPTS


def test_network_error_on_get(account, monkeypatch):
    from instagram_mcp import auth, graph_client
    monkeypatch.setattr(graph_client, "_sleep", lambda s: None)
    account.route("GET", r"^/X$", httpx.RemoteProtocolError("reset"))
    client, _ = auth.client_for(None)
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.get("X")
    assert exc.value.error_class == "network" and "uncertain" not in exc.value.extra


@pytest.mark.parametrize("headers, expected_pct", [
    ({"X-App-Usage": "not json"}, None),
    ({"X-Business-Use-Case-Usage": "[1,2]"}, None),
    ({"X-App-Usage": json.dumps({"call_count": "lots", "total_time": 12})}, 12),
])
def test_malformed_usage_headers_ignored(headers, expected_pct):
    from instagram_mcp import graph_client
    graph_client.LAST_USAGE.clear()
    graph_client._record_usage(headers)  # must never raise
    assert graph_client.LAST_USAGE.get("max_percent") == expected_pct


def test_appsecret_proof_sent_and_page_client_uses_page_token(dm_account, monkeypatch):
    import hashlib
    import hmac

    from instagram_mcp import auth, server
    secret = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("INSTAGRAM_MCP_APP_SECRET", secret)
    auth.clear_cache()
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", {"data": []})
    call(server.list_conversations)()
    def proof(tok):
        return hmac.new(secret.encode(), tok.encode(), hashlib.sha256).hexdigest()
    me = dm_account.calls("GET", r"^/me/accounts$")[0]
    conv = dm_account.calls("GET", r"/conversations$")[0]
    assert me.params["appsecret_proof"] == proof(FAKE_TOKEN)
    assert conv.params["appsecret_proof"] == proof(FAKE_PAGE_TOKEN)


def test_host_drift_refused():
    from instagram_mcp import graph_client
    client = graph_client.GraphClient("EAAx", base_host="graph.facebook.com")
    client._base = "evil.example.com"
    with pytest.raises(graph_client.GraphAPIError) as exc:
        client.get("me")
    assert exc.value.error_class == "ssrf_blocked"


# ------------------------------- server.py ---------------------------------- #

def test_publish_story_image_and_video(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "FINISHED"})
    account.route("POST", rf"^/{FAKE_IG_ID}/media_publish$", {"id": "M1"})
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"username": "demo"})
    assert call(server.publish_story)(image_url="https://cdn.example.com/s.jpg")["media_id"] == "M1"
    assert call(server.publish_story)(video_url="https://cdn.example.com/s.mp4")["media_id"] == "M1"
    forms = [c.form for c in account.calls("POST", rf"^/{FAKE_IG_ID}/media$")]
    assert all(f["media_type"] == "STORIES" for f in forms) and "video_url" in forms[1]
    assert call(server.publish_story)()["error_class"] == "validation"


def test_comment_write_tools(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}$", {"username": "demo"})
    account.route("POST", r"^/c1/replies$", {"id": "r9"})
    account.route("POST", r"^/c1$", {"success": True})
    account.route("DELETE", r"^/c1$", {"success": True})
    assert call(server.reply_to_comment)("c1", "Thanks!")["comment_id"] == "r9"
    assert call(server.hide_comment)("c1")["hidden"] is True
    assert call(server.hide_comment)("c1", hide=False)["hidden"] is False
    assert call(server.delete_comment)("c1")["deleted"] is True
    hides = [c.form.get("hide") for c in account.calls("POST", r"^/c1$")]
    assert hides == ["true", "false"]
    assert call(server.reply_to_comment)("c1", "x" * 2201)["error_class"] == "validation"


def test_healthcheck_live_check_failure(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}$", (400, {"error": {"message": "bad", "code": 190}}))
    res = call(server.healthcheck)()
    assert res["ok"] is False and res["live_check"]["error_class"] == "oauth"


def test_participants_error_does_not_fail_listing(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", rf"^/{FAKE_PAGE_ID}/conversations$", {"data": [{"id": "c1", "updated_time": "t"}]})
    dm_account.route("GET", r"^/c1$", (400, {"error": {"message": "nope", "code": 100}}))
    res = call(server.list_conversations)()
    assert res["ok"] is True and "participants_error" in res["conversations"][0]


def test_explicit_account_with_two_stored_accounts(offline, fake_graph):
    from instagram_mcp import server
    offline.add_account("a", "EAAtokenA0000000000000000000000", "17841400000000001")
    offline.add_account("b", "EAAtokenB0000000000000000000000", "17841400000000002")
    fake_graph.route("GET", r"^/17841400000000002$", {"username": "bee"})
    res = call(server.get_profile)(account="b")
    assert res["profile"]["username"] == "bee"
    assert fake_graph.requests[0].token == "EAAtokenB0000000000000000000000"


# ---------------------------- strengthened ---------------------------------- #

def test_dm_tools_gated_make_no_requests(fake_graph, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("INSTAGRAM_MCP_IG_USER_ID", FAKE_IG_ID)
    from instagram_mcp import server
    for res in (call(server.list_conversations)(), call(server.get_messages)("c1"),
                call(server.send_message)("921", "x"), call(server.mark_seen)("921")):
        assert res["error_class"] == "needs_app_review"
    assert fake_graph.requests == []


def test_dm_enabled_without_account_is_auth_error(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_MCP_DM_ENABLED", "1")
    from instagram_mcp import server
    assert call(server.list_conversations)()["error_class"] == "auth"


# ------------------------------ validators.py ------------------------------- #

@pytest.mark.parametrize("fn", ["validate_ig_user_id", "validate_graph_id", "validate_hashtag", "validate_username",
                                "validate_account_label", "validate_public_https_url", "validate_conversation_id"])
def test_validators_reject_non_strings(fn):
    from instagram_mcp import validators as V
    with pytest.raises(V.ValidationError):
        getattr(V, fn)(12345)


def test_validator_edge_cases():
    from instagram_mcp import validators as V
    assert V.validate_collaborators("brand_a, @brand.b") == '["brand_a", "brand.b"]'
    for bad in ("bad name!", ["ok", "x y"]):
        with pytest.raises(V.ValidationError):
            V.validate_collaborators(bad)
    with pytest.raises(V.ValidationError):
        V.validate_user_tags([])
    with pytest.raises(V.ValidationError):
        V.validate_cursor("a/b")
    with pytest.raises(V.ValidationError):
        V.validate_date(20261001, field="since")
    with pytest.raises(V.ValidationError):
        V.validate_date("2026-13-45T10:00", field="since")
    assert V.validate_date("2026-11-01T10:30:00Z").isoformat() == "2026-11-01T10:30:00+00:00"
    assert V.validate_date("2026-11-01 10:30").tzinfo is not None  # naive -> UTC
    assert V.validate_enum(None, {"a"}, field="f", default="a") == "a"
    with pytest.raises(V.ValidationError):
        V.validate_enum(5, {"a"}, field="f")


def test_unused_slug_validator_removed():
    from instagram_mcp import validators as V
    assert not hasattr(V, "validate_slug")
