"""Stage 11: prepare_only creates + processes the post at Meta but never publishes it."""

from __future__ import annotations

import pytest

from tests.conftest import FAKE_IG_ID, call

CREATE = rf"^/{FAKE_IG_ID}/media$"
PUBLISH = rf"^/{FAKE_IG_ID}/media_publish$"


@pytest.fixture
def meta(account):
    n = {"i": 0}

    def create(req):
        n["i"] += 1
        return {"id": f"C{n['i']}"}

    account.route("POST", CREATE, create)
    account.route("POST", PUBLISH, {"id": "M1"})
    account.route("GET", r"^/C\d+$", {"status_code": "FINISHED"})
    return account


CASES = [
    ("publish_image", ("https://cdn.example.com/a.jpg",), {}),
    ("publish_video", ("https://cdn.example.com/v.mp4",), {}),
    ("publish_reel", ("https://cdn.example.com/v.mp4",), {}),
    ("publish_carousel", (["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.mp4"],), {}),
    ("publish_story", (), {"image_url": "https://cdn.example.com/s.jpg"}),
]


@pytest.mark.parametrize("tool, args, kwargs", CASES)
def test_prepare_only_never_publishes(meta, tool, args, kwargs):
    from instagram_mcp import server
    res = call(getattr(server, tool))(*args, prepare_only=True, **kwargs)
    assert res["ok"] is True, res
    assert res["prepared"] is True and res["published"] is False
    assert res["container_id"].startswith("C") and res["status_code"] == "FINISHED"
    assert "publish_container" in res["hint"]
    assert meta.calls("POST", PUBLISH) == []
    # the container was checked at Meta (processing finished), not just created
    assert meta.calls("GET", rf"^/{res['container_id']}$")


@pytest.mark.parametrize("tool, args, kwargs", CASES)
def test_default_still_publishes(meta, tool, args, kwargs):
    from instagram_mcp import server
    res = call(getattr(server, tool))(*args, **kwargs)
    assert res["ok"] is True and res.get("media_id") == "M1"
    assert len(meta.calls("POST", PUBLISH)) == 1


def test_prepare_only_reports_processing_error(meta):
    from instagram_mcp import server
    meta.route("GET", r"^/C\d+$", {"status_code": "ERROR", "status": "Unsupported image"})
    res = call(server.publish_image)("https://cdn.example.com/a.jpg", prepare_only=True)
    assert res["ok"] is False and "Unsupported image" in res["error"]
