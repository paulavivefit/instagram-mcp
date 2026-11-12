"""Stage 11b: fixes for problems found by the real-API e2e run (2026-11-12)."""

from __future__ import annotations

import json

from tests.conftest import FAKE_IG_ID, call

CREATE = rf"^/{FAKE_IG_ID}/media$"


def test_publish_video_uses_reels_type_shown_in_feed(account):
    """Meta rejects media_type=VIDEO for feed posts (subcode 2207067); videos are reels."""
    from instagram_mcp import server
    account.route("POST", CREATE, {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "FINISHED"})
    res = call(server.publish_video)("https://cdn.example.com/v.mp4", prepare_only=True)
    assert res["ok"] is True, res
    form = account.calls("POST", CREATE)[0].form
    assert form["media_type"] == "REELS" and form["share_to_feed"] == "true"


def test_untaggable_user_is_a_clear_parameter_error_not_oauth(account):
    from instagram_mcp import server
    account.route("POST", CREATE, (400, {"error": {"message": "Invalid user id", "type": "OAuthException",
                                                   "code": 110, "error_subcode": 2207018}}))
    res = call(server.publish_image)("https://cdn.example.com/a.jpg", user_tags=[{"username": "priv", "x": 0.1, "y": 0.1}])
    assert res["ok"] is False and res["error_class"] == "invalid_param"
    assert "public" in res["hint"].lower()


def test_untaggable_user_does_not_clear_dm_page_cache(dm_account):
    from instagram_mcp import server
    dm_account.route("POST", r"/messages$", {"message_id": "m"})
    dm_account.route("POST", CREATE, (400, {"error": {"message": "Invalid user id", "type": "OAuthException",
                                                      "code": 110, "error_subcode": 2207018}}))
    call(server.send_message)("921", "a")
    call(server.publish_image)("https://cdn.example.com/a.jpg", collaborators=["priv"])
    call(server.send_message)("921", "b")
    assert len(dm_account.calls("GET", r"^/me/accounts$")) == 1


def test_video_processing_error_2207076_has_hint(account):
    from instagram_mcp import server
    account.route("POST", CREATE, {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "ERROR",
                                     "status": "Error: Media upload has failed with error code 2207076"})
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", prepare_only=True)
    assert res["ok"] is False
    assert "direct" in res["hint"].lower() and "instagram" in res["hint"].lower()


def test_public_content_access_error_has_hint(account):
    from instagram_mcp import server
    account.route("GET", r"^/ig_hashtag_search$", (400, {"error": {
        "message": "(#10) To use 'Instagram Public Content Access', your use of this endpoint must be "
                   "reviewed and approved by Facebook.", "code": 10}}))
    res = call(server.search_hashtag)("fitness")
    assert res["ok"] is False and res["error_class"] == "permission"
    assert "Instagram Public Content Access" in res["hint"] and "App Review" in res["hint"]


def test_real_oauth_errors_still_oauth(account):
    from instagram_mcp import server
    account.route("GET", rf"^/{FAKE_IG_ID}$", (400, {"error": {"message": "Error validating access token",
                                                                "type": "OAuthException", "code": 190}}))
    assert call(server.get_profile)()["error_class"] == "oauth"


def test_json_tags_unchanged():
    from instagram_mcp import validators as V
    assert json.loads(V.validate_user_tags([{"username": "instagram", "x": 0.5, "y": 0.5}]))[0]["username"] == "instagram"


def test_instagram_cdn_video_links_rejected_up_front(account):
    from instagram_mcp import server
    for url in ("https://scontent-waw2-1.cdninstagram.com/o1/v/t2/f2/m86/x.mp4?efg=1",
                "https://video.xx.fbcdn.net/v/t42/x.mp4"):
        res = call(server.publish_reel)(url, prepare_only=True)
        assert res["ok"] is False and res["error_class"] == "validation"
        assert "own CDN" in res["error"]
    res = call(server.publish_carousel)(["https://cdn.example.com/1.jpg",
                                         "https://scontent.cdninstagram.com/v.mp4"], prepare_only=True)
    assert res["ok"] is False and res["error_class"] == "validation"
    assert account.calls("POST", r"/media$") == []


def test_instagram_cdn_image_links_still_allowed(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", {"id": "C1"})
    account.route("GET", r"^/C1$", {"status_code": "FINISHED"})
    res = call(server.publish_image)("https://scontent.cdninstagram.com/x.jpg", prepare_only=True)
    assert res["ok"] is True, res


def test_location_without_address_has_hint(account):
    from instagram_mcp import server
    account.route("POST", rf"^/{FAKE_IG_ID}/media$", (400, {"error": {"message": "Invalid parameter", "code": 100,
                                                                     "error_subcode": 2207019}}))
    res = call(server.publish_image)("https://cdn.example.com/a.jpg", location_id="123")
    assert res["ok"] is False and "location" in res["hint"].lower()
