"""Real-API end-to-end test of ALL tools over the MCP stdio protocol (like Claude Desktop).

Phases (default: read,local,prepare: no public effect):
  read     every read-only tool against the default account
  local    add/set_default/remove a temporary account (isolated accounts file)
  prepare  every publish tool with prepare_only=True (uploaded + processed, NOT published)
  comments reply_to_comment -> hide -> unhide -> delete on our own post     [public, needs --approve]
  dm       send_message text+image, mark_seen, typing to --dm-to username   [needs --approve]
  publish  publish_container on a prepared story (REALLY PUBLISHES)        [needs --approve-publish]

Prints only ok/fail, counts and ids: no tokens, captions or message text.

    python scripts/e2e_live.py --server .venv/bin/instagram-mcp [--phases read,local,prepare]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

RESULTS: list[tuple[str, str, bool, str, float]] = []
# Public CC0 sample video (Meta rejects Instagram's own CDN links as video sources).
SAMPLE_VIDEO = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
TAG_USER = "instagram"  # tags/collaborators must be public accounts


class MCP:
    def __init__(self, binary: str, env: dict[str, str]):
        self.p = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=env)
        self.i = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "e2e-live", "version": "1"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()
        self.tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.i, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            msg = json.loads(line)
            if msg.get("id") == self.i:
                return msg

    def call(self, phase: str, name: str, args: dict[str, Any], show=lambda d: "",
             expect_ok: bool = True) -> dict[str, Any]:
        start = time.monotonic()
        msg = self.rpc("tools/call", {"name": name, "arguments": args})
        if "error" in msg:
            data = {"ok": False, "error": msg["error"].get("message"), "error_class": "protocol"}
        else:
            res = msg["result"]
            text = res["content"][0]["text"] if res.get("content") else "{}"
            try:
                data = json.loads(text)
            except ValueError:
                data = {"ok": False, "error": text[:200], "error_class": "protocol"}
            if res.get("isError") and data.get("ok") is not False:
                data = {"ok": False, "error": text[:200], "error_class": "protocol"}
        ok = bool(data.get("ok")) == expect_ok
        try:
            detail = show(data) if data.get("ok") else f"{data.get('error_class')}: {str(data.get('error'))[:110]}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"unexpected result shape ({type(exc).__name__})"
        label = name if expect_ok else f"{name} (expected refusal)"
        RESULTS.append((phase, label, ok, detail, time.monotonic() - start))
        return data

    def close(self) -> None:
        self.p.kill()


def keychain_token(label: str) -> str:
    return subprocess.run(["security", "find-generic-password", "-a", label, "-s", "instagram-mcp", "-w"],
                          capture_output=True, text=True, check=True).stdout.strip()


def phase_read(m: MCP, ctx: dict[str, Any]) -> None:
    P = "read"
    m.call(P, "healthcheck", {}, lambda d: f"{d['live_check']['username']} {d['graph_version']} usage={d.get('api_usage', {}).get('max_percent')}%")
    m.call(P, "list_accounts", {}, lambda d: f"{len(d['accounts'])} account(s)")
    m.call(P, "account_info", {}, lambda d: "ok")
    prof = m.call(P, "get_profile", {}, lambda d: f"media_count={d['profile']['media_count']}")
    ctx["username"] = (prof.get("profile") or {}).get("username")
    allm = m.call(P, "list_media", {"limit": "all"}, lambda d: f"{d['count']} posts")
    media = allm.get("media", [])
    m.call(P, "list_media", {"limit": 5, "since": "2026-09-01", "until": "2026-10-31"},
           lambda d: f"{d['count']} posts in range")
    page1 = m.call(P, "list_media", {"limit": 120}, lambda d: f"{d['count']} + cursor={bool(d.get('next_cursor'))}")
    if page1.get("next_cursor"):
        m.call(P, "list_media", {"limit": 50, "after": page1["next_cursor"]}, lambda d: f"{d['count']} (resumed)")
    image = next((x for x in media if x.get("media_type") == "IMAGE" and x.get("media_url")), None)
    carousel = next((x for x in media if x.get("media_type") == "CAROUSEL_ALBUM"), None)
    reel = next((x for x in media if x.get("media_product_type") == "REELS" and x.get("media_url")), None)
    busiest = max(media[:60], key=lambda x: x.get("comments_count", 0), default=None)
    ctx.update(image=image, reel=reel, carousel=carousel, busiest=busiest, media=media)
    if carousel:
        g = m.call(P, "get_media", {"media_id": carousel["id"]},
                   lambda d: f"{len(d['media']['children']['data'])} children")
        ctx["carousel_children"] = ((g.get("media") or {}).get("children") or {}).get("data", [])
        m.call(P, "get_media_insights", {"media_id": carousel["id"]}, lambda d: f"{len(d['insights'])} metrics (FEED)")
    if reel:
        m.call(P, "get_media_insights", {"media_id": reel["id"]}, lambda d: f"{len(d['insights'])} metrics (REELS)")
    m.call(P, "list_stories", {"include_insights": True}, lambda d: f"{d['count']} live")
    m.call(P, "get_account_insights", {}, lambda d: f"{len(d['insights'])} metrics, errors={len(d.get('errors', []))}")
    m.call(P, "get_account_insights", {"since": "2026-07-01", "until": "2026-10-31", "period": "day"},
           lambda d: f"{len(d['insights'])} metrics, errors={len(d.get('errors', []))}")
    m.call(P, "get_audience_insights", {}, lambda d: f"{sorted(d['breakdowns'])}")
    m.call(P, "get_audience_insights", {"metric": "engaged_audience_demographics", "breakdown": "age",
                                        "timeframe": "this_month"}, lambda d: f"{sorted(d['breakdowns'])}")
    m.call(P, "top_posts", {"since": "2026-09-01", "metric": "reach", "n": 3}, lambda d: f"{d['posts_considered']} ranked")
    m.call(P, "top_posts", {"metric": "views", "n": 3, "media_type": "REELS", "since": "2026-01-01"},
           lambda d: f"{d['posts_considered']} reels ranked")
    for per in ("last_7_days", "last_30_days", "last_90_days"):
        m.call(P, "insights_summary", {"period": per}, lambda d: f"followers={d['followers_now']}")
    m.call(P, "publishing_limit", {}, lambda d: f"{d['limit'][0]['quota_usage']}/{d['limit'][0]['config']['quota_total']}")
    if busiest:
        c = m.call(P, "get_comments", {"media_id": busiest["id"], "limit": "all", "include_replies": True},
                   lambda d: f"{d['count']} + {d['reply_count']} replies")
        parent = next((x for x in c.get("comments", []) if x.get("replies")), None)
        if parent:
            m.call(P, "get_comment_replies", {"comment_id": parent["id"], "limit": "all"}, lambda d: f"{d['count']} replies")
    m.call(P, "get_mentions", {"limit": 30}, lambda d: f"{d['count']} tagged")
    h = m.call(P, "search_hashtag", {"hashtag": "fitness"}, lambda d: f"{len(d['results'])} id(s)")
    if not h.get("ok") and "Instagram Public Content Access" in str(h.get("hint")):
        RESULTS[-1] = (P, "search_hashtag (needs Meta feature)", True,
                       "permission refused by Meta as expected; hint explains App Review", RESULTS[-1][4])
    if h.get("results"):
        hid = h["results"][0]["id"]
        m.call(P, "get_hashtag_media", {"hashtag_id": hid, "edge": "top_media", "limit": 30}, lambda d: f"{d['count']} top")
        m.call(P, "get_hashtag_media", {"hashtag_id": hid, "edge": "recent_media", "limit": 30}, lambda d: f"{d['count']} recent")
    m.call(P, "business_discovery", {"username": ctx["username"], "with_media": True, "media_limit": 150},
           lambda d: f"{len(d['business_discovery']['media']['data'])} posts")
    conv = m.call(P, "list_conversations", {"limit": 3}, lambda d: f"{d['count']} conversation(s)")
    if conv.get("conversations"):
        ctx["conversation"] = conv["conversations"][0]
        m.call(P, "get_messages", {"conversation_id": conv["conversations"][0]["id"], "limit": "all"},
               lambda d: f"{len(d['messages'])} messages")
    # refusals: validation must stop these before Meta
    m.call(P, "list_media", {"limit": 0}, expect_ok=False)
    m.call(P, "top_posts", {"metric": "impressions"}, expect_ok=False)
    m.call(P, "get_messages", {"conversation_id": "../me"}, expect_ok=False)


def phase_local(binary: str, base_env: dict[str, str], ig_id: str) -> None:
    P = "local"
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(base_env, INSTAGRAM_MCP_ACCOUNTS_PATH=os.path.join(tmp, "accounts.json"))
        m = MCP(binary, env)
        label = "e2e-temp"
        try:
            m.call(P, "add_account", {"label": label, "access_token": keychain_token("vivefit"),
                                      "ig_user_id": ig_id, "make_default": False}, lambda d: "stored")
            m.call(P, "set_default_account", {"label": label}, lambda d: "default set")
            m.call(P, "list_accounts", {}, lambda d: f"default={[a['label'] for a in d['accounts'] if a.get('default')]}")
            m.call(P, "healthcheck", {}, lambda d: f"via temp account: {d['live_check']['username']}")
            m.call(P, "get_profile", {"account": label}, lambda d: "explicit account arg ok")
            m.call(P, "add_account", {"label": "bad label!", "access_token": "x", "ig_user_id": "1"}, expect_ok=False)
        finally:
            m.call(P, "remove_account", {"label": label}, lambda d: "removed + token deleted")
            m.close()
        gone = subprocess.run(["security", "find-generic-password", "-a", label, "-s", "instagram-mcp"],
                              capture_output=True).returncode != 0
        RESULTS.append((P, "keychain cleaned up", gone, "temp token absent" if gone else "TEMP TOKEN STILL IN KEYCHAIN", 0))


def phase_prepare(m: MCP, ctx: dict[str, Any]) -> None:
    P = "prepare"
    img = (ctx.get("image") or {}).get("media_url")
    vid = SAMPLE_VIDEO
    if not img:
        RESULTS.append((P, "source media", False, "no image/reel media_url found", 0))
        return
    show = lambda d: f"container {d['container_id']} {d['status_code']}, published={d['published']}"  # noqa: E731
    m.call(P, "publish_image", {"image_url": img, "caption": "e2e test #test", "alt_text": "Test image",
                                "user_tags": [{"username": TAG_USER, "x": 0.5, "y": 0.5}],
                                "collaborators": [TAG_USER], "prepare_only": True}, show)
    m.call(P, "publish_image", {"image_url": img, "prepare_only": True}, show)
    m.call(P, "publish_reel", {"video_url": vid, "caption": "e2e", "share_to_feed": False, "thumb_offset": 1000,
                               "audio_name": "e2e audio", "user_tags": [{"username": TAG_USER}],
                               "prepare_only": True}, show)
    m.call(P, "publish_video", {"video_url": vid, "caption": "e2e", "prepare_only": True}, show)
    m.call(P, "publish_carousel", {"image_urls": [img, img, vid], "caption": "e2e", "prepare_only": True}, show)
    story = m.call(P, "publish_story", {"image_url": img, "prepare_only": True}, show)
    ctx["story_container"] = story.get("container_id")
    m.call(P, "publish_image", {"image_url": "http://example.com/a.jpg", "prepare_only": True}, expect_ok=False)
    m.call(P, "publish_image", {"image_url": img, "caption": " ".join(f"#t{i}" for i in range(31)),
                                "prepare_only": True}, expect_ok=False)
    m.call(P, "publish_container", {"container_id": "1"}, expect_ok=False)
    m.call(P, "publish_image", {"image_url": img, "user_tags": [{"username": "bnancore", "x": 0.1, "y": 0.1}],
                                "prepare_only": True}, expect_ok=False)
    cdn_vid = (ctx.get("reel") or {}).get("media_url")
    if cdn_vid:
        m.call(P, "publish_reel", {"video_url": cdn_vid, "prepare_only": True}, expect_ok=False)


def phase_comments(m: MCP, ctx: dict[str, Any]) -> None:
    P = "comments"
    own = next((x for x in ctx.get("media", []) if x.get("comments_count", 0) >= 0), None)
    c = m.call(P, "get_comments", {"media_id": own["id"], "limit": 5}, lambda d: f"{d['count']}")
    target = (c.get("comments") or [None])[0]
    if not target:
        RESULTS.append((P, "target comment", False, "no comment to reply to", 0))
        return
    r = m.call(P, "reply_to_comment", {"comment_id": target["id"], "message": "Test, will be removed."},
               lambda d: f"reply {d.get('comment_id') or d.get('id')}")
    rid = r.get("comment_id") or r.get("id") or (r.get("result") or {}).get("id")
    if rid:
        m.call(P, "hide_comment", {"comment_id": rid, "hide": True}, lambda d: "hidden")
        m.call(P, "hide_comment", {"comment_id": rid, "hide": False}, lambda d: "unhidden")
        m.call(P, "delete_comment", {"comment_id": rid}, lambda d: "deleted")


def phase_dm(m: MCP, ctx: dict[str, Any], to: str) -> None:
    P = "dm"
    conv = ctx.get("conversation") or {}
    rid = next((p["id"] for p in conv.get("participants", []) if p.get("username") == to), None)
    if not rid:
        RESULTS.append((P, "recipient", False, f"{to} not found in latest conversation", 0))
        return
    img = (ctx.get("image") or {}).get("media_url")
    m.call(P, "mark_seen", {"recipient_id": rid, "action": "typing_on"}, lambda d: "typing on")
    m.call(P, "send_message", {"recipient_id": rid, "text": "E2E test message (automated)."},
           lambda d: f"{len(d['message_ids'])} sent")
    m.call(P, "send_message", {"recipient_id": rid, "text": "E2E test with image", "image_url": img},
           lambda d: f"{len(d['message_ids'])} sent")
    m.call(P, "mark_seen", {"recipient_id": rid, "action": "typing_off"}, lambda d: "typing off")
    m.call(P, "mark_seen", {"recipient_id": rid}, lambda d: "seen")
    m.call(P, "send_message", {"recipient_id": rid, "text": "x", "tag": "PROMO"}, expect_ok=False)


def phase_publish(m: MCP, ctx: dict[str, Any]) -> None:
    cid = ctx.get("story_container")
    if not cid:
        RESULTS.append(("publish", "story container", False, "prepare phase did not run", 0))
        return
    m.call("publish", "publish_container", {"container_id": cid}, lambda d: f"published media {d['media_id']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--phases", default="read,local,prepare")
    ap.add_argument("--dm-to", default="bnancore")
    ap.add_argument("--approve", action="store_true", help="allow public comment + DM phases")
    ap.add_argument("--approve-publish", action="store_true", help="allow REAL publishing of a story")
    a = ap.parse_args()
    phases = a.phases.split(",")
    if {"comments", "dm"} & set(phases) and not a.approve:
        sys.exit("comments/dm phases need --approve")
    if "publish" in phases and not a.approve_publish:
        sys.exit("publish phase needs --approve-publish")
    env = dict(os.environ, INSTAGRAM_MCP_DM_ENABLED="1")
    m = MCP(a.server, env)
    ctx: dict[str, Any] = {}
    print(f"tools exposed: {len(m.tools)}")
    try:
        phase_read(m, ctx)  # always: other phases need its context
        if "local" in phases:
            phase_local(a.server, env, "17841401555704149")
        if "prepare" in phases or "publish" in phases:
            phase_prepare(m, ctx)
        if "comments" in phases:
            phase_comments(m, ctx)
        if "dm" in phases:
            phase_dm(m, ctx, a.dm_to)
        if "publish" in phases:
            phase_publish(m, ctx)
    finally:
        m.close()
    called = {r[1].split(" ")[0] for r in RESULTS}
    w = max(len(r[1]) for r in RESULTS)
    for phase, name, ok, detail, secs in RESULTS:
        print(f"{'OK  ' if ok else 'FAIL'} [{phase:8}] {name:<{w}} {secs:5.1f}s  {detail}")
    failed = [r[1] for r in RESULTS if not r[2]]
    untested = sorted(set(m.tools) - called)
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; tools exercised "
          f"{len(set(m.tools) & called)}/{len(m.tools)}")
    if untested:
        print("not exercised in this run:", ", ".join(untested))
    if failed:
        print("FAILED:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
