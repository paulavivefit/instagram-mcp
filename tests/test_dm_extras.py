"""Stage 7: DM extras (fake Graph only: nothing is ever sent for real)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FAKE_PAGE_ID, call

SEND = rf"^/{FAKE_PAGE_ID}/messages$"


@pytest.fixture
def sending(dm_account):
    dm_account.route("POST", SEND, {"recipient_id": "921", "message_id": "mid.1"})
    return dm_account


def _sent(fake):
    return fake.calls("POST", SEND)[-1].form


def test_plain_reply_is_a_response(sending):
    from instagram_mcp import server
    assert call(server.send_message)("921", "Hi")["ok"] is True
    form = _sent(sending)
    assert form["messaging_type"] == "RESPONSE"
    assert "tag" not in form


def test_human_agent_tag(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921", "Sorry for the late answer", tag="HUMAN_AGENT")
    assert res["ok"] is True, res
    form = _sent(sending)
    assert form["messaging_type"] == "MESSAGE_TAG" and form["tag"] == "HUMAN_AGENT"


def test_unknown_tag_rejected(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921", "x", tag="PROMO")
    assert res["ok"] is False and res["error_class"] == "validation"
    assert sending.calls("POST", SEND) == []


def test_image_attachment(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921", image_url="https://cdn.example.com/plan.jpg")
    assert res["ok"] is True, res
    msg = json.loads(_sent(sending)["message"])
    assert msg == {"attachment": {"type": "image", "payload": {"url": "https://cdn.example.com/plan.jpg"}}}


def test_text_and_image_sends_two_messages(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921", "Here is your plan", image_url="https://cdn.example.com/plan.jpg")
    assert res["ok"] is True, res
    bodies = [json.loads(c.form["message"]) for c in sending.calls("POST", SEND)]
    assert bodies[0] == {"text": "Here is your plan"} and "attachment" in bodies[1]
    assert len(res["message_ids"]) == 2


def test_send_needs_text_or_image(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921")
    assert res["ok"] is False and res["error_class"] == "validation"


def test_text_length_limit(sending):
    from instagram_mcp import server
    res = call(server.send_message)("921", "x" * 1001)
    assert res["ok"] is False and "1000" in res["error"]


def test_outside_window_error_gets_clear_hint(dm_account):
    from instagram_mcp import server
    dm_account.route("POST", SEND, (400, {"error": {
        "message": "(#10) This message is sent outside of allowed window.", "code": 10,
        "error_subcode": 2534022}}))
    res = call(server.send_message)("921", "late reply")
    assert res["ok"] is False
    assert res["error_class"] == "outside_window"
    assert "HUMAN_AGENT" in res["hint"]


def test_mark_seen_and_typing(sending):
    from instagram_mcp import server
    assert call(server.mark_seen)("921")["ok"] is True
    form = _sent(sending)
    assert form["sender_action"] == "mark_seen" and json.loads(form["recipient"]) == {"id": "921"}
    assert "message" not in form
    assert call(server.mark_seen)("921", action="typing_on")["ok"] is True
    assert _sent(sending)["sender_action"] == "typing_on"
    res = call(server.mark_seen)("921", action="dance")
    assert res["ok"] is False and res["error_class"] == "validation"


def test_get_messages_requests_attachments_and_shares(dm_account):
    from instagram_mcp import server
    dm_account.route("GET", r"^/conv1$", {"messages": {"data": [
        {"id": "m1", "attachments": {"data": [{"generic_template": {"title": "Auto reply"}}]}}]}})
    res = call(server.get_messages)("conv1")
    fields = dm_account.calls("GET", r"^/conv1$")[0].params["fields"]
    for f in ("attachments", "shares", "story", "reactions"):
        assert f in fields
    assert res["messages"][0]["attachments"]["data"][0]["generic_template"]["title"] == "Auto reply"


def test_dm_extras_still_gated(account):
    from instagram_mcp import server
    res = call(server.mark_seen)("921")
    assert res["ok"] is False and res["error_class"] == "needs_app_review"
