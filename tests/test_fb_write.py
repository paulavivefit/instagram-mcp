"""Write tools: publishing (now, scheduled, dry run), edit/delete, comments, moderation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tests.conftest import FB_PAGE_ID, call

POST = f"{FB_PAGE_ID}_100"


def _srv():
    from facebook_mcp import server
    return server


def _in(**kw):
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


def test_create_post_now_returns_permalink(fb_account):
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/feed$", {"id": f"{FB_PAGE_ID}_1"})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_1$", {"permalink_url": "https://fb/p/1"})
    out = call(_srv().create_post)(message="Hej!", link="https://vivefit.pl")
    assert out["ok"] and out["status"] == "published" and out["permalink_url"] == "https://fb/p/1"
    form = fb_account.calls("POST", r"/feed$")[0].form
    assert form == {"message": "Hej!", "link": "https://vivefit.pl"}
    assert out["page_name"] == "Trening Fitness"


def test_create_post_scheduled(fb_account):
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/feed$", {"id": f"{FB_PAGE_ID}_2"})
    out = call(_srv().create_post)(message="later", scheduled_time=_in(days=2))
    assert out["status"] == "scheduled" and "scheduled_for" in out
    form = fb_account.calls("POST", r"/feed$")[0].form
    assert form["published"] == "false" and int(form["scheduled_publish_time"]) > 0


def test_schedule_window_and_format(fb_account):
    srv = _srv()
    assert call(srv.create_post)(message="x", scheduled_time=_in(minutes=2))["error_class"] == "validation"
    assert call(srv.create_post)(message="x", scheduled_time=_in(days=40))["error_class"] == "validation"
    assert call(srv.create_post)(message="x", scheduled_time="2026-12-01")["error_class"] == "validation"
    assert not fb_account.calls("POST", r".")


def test_dry_run_sends_nothing(fb_account):
    out = call(_srv().create_post)(message="preview", dry_run=True)
    assert out["dry_run"] and out["would_send"]["endpoint"] == f"{FB_PAGE_ID}/feed"
    out = call(_srv().create_photo_post)(image_urls=["https://a.pl/1.jpg", "https://a.pl/2.jpg"], dry_run=True)
    assert len(out["would_send"]) == 3
    out = call(_srv().create_video_post)(video_url="https://a.pl/v.mp4", dry_run=True)
    assert out["would_send"]["fields"]["file_url"] == "https://a.pl/v.mp4"
    assert not fb_account.calls("POST", r".")


def test_message_validation(fb_account):
    srv = _srv()
    assert call(srv.create_post)(message="  ")["error_class"] == "validation"
    assert call(srv.create_post)(message="x" * 63207)["error_class"] == "validation"
    assert call(srv.create_post)(message="x", link="ftp://a")["error_class"] == "validation"


def test_single_photo(fb_account):
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/photos$", {"id": "77", "post_id": f"{FB_PAGE_ID}_77"})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_77$", {"permalink_url": "https://fb/p/77"})
    out = call(_srv().create_photo_post)(image_urls=["https://a.pl/1.jpg"], message="cap")
    assert out["post_id"] == f"{FB_PAGE_ID}_77" and out["permalink_url"] == "https://fb/p/77"
    assert fb_account.calls("POST", r"/photos$")[0].form == {"url": "https://a.pl/1.jpg", "caption": "cap"}


def test_single_photo_scheduled(fb_account):
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/photos$", {"id": "78", "post_id": f"{FB_PAGE_ID}_78"})
    call(_srv().create_photo_post)(image_urls=["https://a.pl/1.jpg"], scheduled_time=_in(days=3))
    form = fb_account.calls("POST", r"/photos$")[0].form
    assert form["unpublished_content_type"] == "SCHEDULED" and form["published"] == "false"


def test_multi_photo_album(fb_account):
    ids = iter(["11", "12"])
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/photos$", lambda r: {"id": next(ids)})
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/feed$", {"id": f"{FB_PAGE_ID}_5"})
    out = call(_srv().create_photo_post)(image_urls=["https://a.pl/1.jpg", "https://a.pl/2.jpg"], message="m",
                                         scheduled_time=_in(days=1))
    assert out["photo_ids"] == ["11", "12"] and out["status"] == "scheduled"
    ups = fb_account.calls("POST", r"/photos$")
    assert all(u.form["published"] == "false" and u.form["temporary"] == "true" for u in ups)
    feed = fb_account.calls("POST", r"/feed$")[0].form
    assert json.loads(feed["attached_media[1]"]) == {"media_fbid": "12"}


def test_photo_url_validation(fb_account):
    srv = _srv()
    assert call(srv.create_photo_post)(image_urls=[])["error_class"] == "validation"
    assert call(srv.create_photo_post)(image_urls=["http://a.pl/1.jpg"])["error_class"] == "validation"
    assert call(srv.create_photo_post)(image_urls=["https://a.pl/x.jpg"] * 11)["error_class"] == "validation"


def test_video(fb_account):
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/videos$", {"id": "900"})
    out = call(_srv().create_video_post)(video_url="https://a.pl/v.mp4", description="d", title="t",
                                         scheduled_time=_in(days=5))
    assert out["id"] == "900" and out["status"] == "scheduled"
    form = fb_account.calls("POST", r"/videos$")[0].form
    assert form["file_url"] == "https://a.pl/v.mp4" and form["published"] == "false"
    assert call(_srv().create_video_post)(video_url="https://a.pl/v.mp4", title="t" * 256)["error_class"] == "validation"


def test_update_and_delete_post(fb_account):
    fb_account.route("POST", rf"^/{POST}$", {"success": True})
    fb_account.route("DELETE", rf"^/{POST}$", {"success": True})
    assert call(_srv().update_post)(post_id=POST, message="new")["updated"] is True
    assert fb_account.calls("POST", rf"^/{POST}$")[0].form == {"message": "new"}
    assert call(_srv().delete_post)(post_id=POST)["deleted"] is True


def test_comment_reply_unhide_delete(fb_account):
    fb_account.route("POST", rf"^/{POST}/comments$", {"id": "100_50"})
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_100$", {"id": POST})
    fb_account.route("POST", r"^/100_50/comments$", {"id": "100_51"})
    fb_account.route("POST", r"^/100_51$", {"success": True})
    fb_account.route("DELETE", r"^/100_51$", {"success": True})
    srv = _srv()
    assert call(srv.comment_on_post)(post_id=POST, message="hi")["comment_id"] == "100_50"
    r = call(srv.reply_to_comment)(comment_id="100_50", message="re")
    assert r["comment_id"] == "100_51" and r["reply_to"] == "100_50"
    assert call(srv.unhide_comment)(comment_id="100_51")["hidden"] is False
    assert fb_account.calls("POST", r"^/100_51$")[0].form == {"is_hidden": "false"}
    assert call(srv.delete_comment)(comment_id="100_51")["deleted"] is True


def test_uncertain_write_hint_on_post_timeout(fb_account):
    import httpx
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/feed$", httpx.ReadTimeout("slow"))
    out = call(_srv().create_post)(message="x")
    assert out["ok"] is False and "hint" in out
    assert len(fb_account.calls("POST", r"/feed$")) == 1  # never retried


def test_audit_records_fb_prefix(fb_account, tmp_path):
    call(_srv().create_post)(message="x", dry_run=True)
    lines = (tmp_path / "audit.log.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["tool"] == "fb.create_post"


def test_album_failure_cleans_up_uploaded_photos(fb_account):
    calls = iter([{"id": "11"}, (400, {"error": {"code": 324, "message": "Missing or invalid image file"}})])
    fb_account.route("POST", rf"^/{FB_PAGE_ID}/photos$", lambda r: next(calls))
    fb_account.route("DELETE", r"^/11$", {"success": True})
    out = call(_srv().create_photo_post)(image_urls=["https://a.pl/1.jpg", "https://b.pl/2.jpg"])
    assert out["ok"] is False and "download" in out["hint"] and out["image_url"] == "https://b.pl/2.jpg"
    assert len(fb_account.calls("DELETE", r"^/11$")) == 1
    assert not fb_account.calls("POST", r"/feed$")


def test_hide_own_comment_explains_meta_rule(fb_account):
    fb_account.route("GET", rf"^/{FB_PAGE_ID}_100$", {"id": POST})
    fb_account.route("POST", r"^/100_7$", (400, {"error": {"code": 200, "message": "Can not hide or unhide this comment"}}))
    out = call(_srv().hide_comment)(comment_id="100_7")
    assert out["error_class"] == "permission" and "other people" in out["hint"]
