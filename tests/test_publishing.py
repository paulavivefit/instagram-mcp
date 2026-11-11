"""Stage 6: publishing options (fake Graph only: nothing is ever posted for real)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FAKE_IG_ID, call

CREATE = rf"^/{FAKE_IG_ID}/media$"
PUBLISH = rf"^/{FAKE_IG_ID}/media_publish$"


@pytest.fixture
def publishing(account):
    counter = {"n": 0}

    def create(req):
        counter["n"] += 1
        return {"id": f"C{counter['n']}"}

    account.route("POST", CREATE, create)
    account.route("POST", PUBLISH, {"id": "M1"})
    account.route("GET", r"^/C\d+$", {"status_code": "FINISHED"})
    return account


def _creates(fake):
    return [c.form for c in fake.calls("POST", CREATE)]


# ------------------------------ caption rules ------------------------------- #

def test_caption_hashtag_and_mention_limits():
    from instagram_mcp import validators as V
    V.validate_caption(" ".join(f"#t{i}" for i in range(30)))
    with pytest.raises(V.ValidationError, match="hashtags"):
        V.validate_caption(" ".join(f"#t{i}" for i in range(31)))
    V.validate_caption(" ".join(f"@u{i}" for i in range(20)))
    with pytest.raises(V.ValidationError, match="mentions"):
        V.validate_caption(" ".join(f"@u{i}" for i in range(21)))
    V.validate_caption("email me: paula@example.com #fit")  # an email is not a mention


# --------------------------------- image ------------------------------------ #

def test_publish_image_with_all_options(publishing):
    from instagram_mcp import server
    res = call(server.publish_image)(
        "https://cdn.example.com/a.jpg", caption="Hi #fit", alt_text="Woman doing a plank",
        user_tags=[{"username": "bnancore", "x": 0.5, "y": 0.25}], location_id="110001",
        collaborators=["partner.brand"])
    assert res["ok"] is True, res
    (form,) = _creates(publishing)
    assert form["alt_text"] == "Woman doing a plank"
    assert json.loads(form["user_tags"]) == [{"username": "bnancore", "x": 0.5, "y": 0.25}]
    assert form["location_id"] == "110001"
    assert json.loads(form["collaborators"]) == ["partner.brand"]


def test_publish_image_plain_sends_no_extra_fields(publishing):
    from instagram_mcp import server
    call(server.publish_image)("https://cdn.example.com/a.jpg", caption="Hi")
    (form,) = _creates(publishing)
    assert set(form) == {"image_url", "caption"}


@pytest.mark.parametrize("kwargs, match", [
    (dict(user_tags=[{"username": "a", "x": 1.5, "y": 0.1}]), "between 0 and 1"),
    (dict(user_tags=[{"username": "bad name", "x": 0.1, "y": 0.1}]), "username"),
    (dict(user_tags=[{"username": f"u{i}", "x": 0.1, "y": 0.1} for i in range(21)]), "20"),
    (dict(collaborators=["a", "b", "c", "d"]), "3"),
    (dict(location_id="abc"), "location_id"),
    (dict(alt_text="x" * 1001), "alt_text"),
])
def test_publish_image_rejects_bad_options_before_calling_meta(publishing, kwargs, match):
    from instagram_mcp import server
    res = call(server.publish_image)("https://cdn.example.com/a.jpg", **kwargs)
    assert res["ok"] is False and res["error_class"] == "validation"
    assert match in res["error"]
    assert publishing.calls("POST", CREATE) == []


# ---------------------------------- reel ------------------------------------ #

def test_publish_reel_options(publishing):
    from instagram_mcp import server
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", caption="Reel", thumb_offset=2500,
                                    collaborators=["coach.anna"], audio_name="ViveFit beat",
                                    location_id="110001", share_to_feed=False,
                                    user_tags=[{"username": "bnancore"}])
    assert res["ok"] is True, res
    (form,) = _creates(publishing)
    assert form["media_type"] == "REELS"
    assert form["thumb_offset"] == "2500"
    assert form["audio_name"] == "ViveFit beat"
    assert form["share_to_feed"] == "false"
    assert json.loads(form["user_tags"]) == [{"username": "bnancore"}]  # reels: no x/y


def test_publish_reel_rejects_negative_thumb_offset(publishing):
    from instagram_mcp import server
    res = call(server.publish_reel)("https://cdn.example.com/v.mp4", thumb_offset=-1)
    assert res["ok"] is False and res["error_class"] == "validation"


# -------------------------------- carousel ---------------------------------- #

def test_carousel_mixed_images_and_videos(publishing):
    from instagram_mcp import server
    res = call(server.publish_carousel)(
        ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.mp4", "https://cdn.example.com/3.png"],
        caption="Mix", collaborators=["partner.brand"], location_id="110001")
    assert res["ok"] is True, res
    forms = _creates(publishing)
    children, parent = forms[:3], forms[3]
    assert children[0]["image_url"].endswith("1.jpg") and children[0]["is_carousel_item"] == "true"
    assert children[1]["media_type"] == "VIDEO" and children[1]["video_url"].endswith("2.mp4")
    assert parent["media_type"] == "CAROUSEL" and parent["children"] == "C1,C2,C3"
    assert json.loads(parent["collaborators"]) == ["partner.brand"]
    # the video child was checked for processing before the parent was created
    assert publishing.calls("GET", r"^/C2$")


def test_carousel_size_limits(publishing, monkeypatch):
    from instagram_mcp import server
    one = ["https://cdn.example.com/1.jpg"]
    assert call(server.publish_carousel)(one)["ok"] is False
    res = call(server.publish_carousel)(one * 11)
    assert res["ok"] is False and "2-10" in res["error"]
    monkeypatch.setattr(server, "CAROUSEL_MAX", 20)
    assert call(server.publish_carousel)(one * 20)["ok"] is True


def test_publish_image_caption_with_too_many_hashtags_never_reaches_meta(publishing):
    from instagram_mcp import server
    res = call(server.publish_image)("https://cdn.example.com/a.jpg",
                                     caption=" ".join(f"#t{i}" for i in range(31)))
    assert res["ok"] is False
    assert publishing.calls("POST", CREATE) == []
