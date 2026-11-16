"""Real-API end-to-end test of ALL facebook-mcp tools over MCP stdio (like Claude Desktop).

Phases (default: read,dryrun: no effect on the Page):
  read      every read tool + the Page lock refusing another Page on the token
  dryrun    every create tool with dry_run=True (nothing is sent)
  hidden    scheduled text/photo/album/video posts 29 days ahead -> list -> edit -> delete
            (scheduled posts are never shown to followers)                        [needs --approve]
  comments  comment_on_post + reply_to_comment on the Page's own comment -> hide -> unhide -> delete
            (briefly visible)                                                      [needs --approve]
  publish   create_post now -> get_post -> delete_post (visible for seconds)      [needs --approve-publish]

Prints only ok/fail, counts and ids: no tokens or post text.

    python scripts/fb_e2e_live.py --server .venv/bin/facebook-mcp [--phases read,dryrun]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_live import MCP, RESULTS  # noqa: E402

IMAGE_1 = "https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg"
IMAGE_2 = IMAGE_1  # Facebook accepts the same file twice; many thumbnail hosts block its fetcher
SAMPLE_VIDEO = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
FORBIDDEN_PAGE = "186933557996398"  # PUMA Klub Fitness: on the token, NOT allowed
E2E_TEXT = "[test ViveFit e2e - will be deleted]"


def later(days: int = 29) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def phase_read(m: MCP, ctx: dict[str, Any]) -> None:
    P = "read"
    hc = m.call(P, "healthcheck", {}, lambda d: f"{d['allowed_pages'][0]['page_name']} missing={d['missing_permissions']}")
    ctx["page_id"] = hc["allowed_pages"][0]["page_id"]
    m.call(P, "list_pages", {}, lambda d: f"{d['count']} page(s) followers={d['pages'][0]['followers_count']}")
    m.call(P, "get_page", {}, lambda d: f"{d['profile']['name']} ig={d['profile'].get('instagram_business_account', {}).get('username')}")
    m.call(P, "get_page", {"page_id": FORBIDDEN_PAGE}, expect_ok=False)
    m.call(P, "get_post", {"post_id": f"{FORBIDDEN_PAGE}_1"}, expect_ok=False)
    posts = m.call(P, "list_posts", {"limit": 30}, lambda d: f"{d['count']} posts cursor={bool(d.get('next_cursor'))}")
    items = posts.get("posts", [])
    if posts.get("next_cursor"):
        m.call(P, "list_posts", {"limit": 5, "after": posts["next_cursor"]}, lambda d: f"{d['count']} (resumed)")
    m.call(P, "list_posts", {"limit": "all", "since": "2026-10-01", "compact": True},
           lambda d: f"{d['count']} posts since Oct (compact)")
    busiest = max(items, key=lambda p: p.get("comments_count", 0), default=None)
    video = next((p for p in items if any("video" in str(a.get("media_type", "")) for a in p.get("attachments", []))),
                 None)
    ctx.update(posts=items, busiest=busiest)
    if busiest:
        m.call(P, "get_post", {"post_id": busiest["id"]}, lambda d: f"comments={d['post']['comments_count']}")
        c = m.call(P, "get_comments", {"post_id": busiest["id"], "limit": "all"},
                   lambda d: f"{d['count']} comments, note={'note' in d}")
        com = c.get("comments", [])
        if com:
            m.call(P, "get_comment", {"comment_id": com[0]["id"]}, lambda d: f"replies={len(d['comment'].get('replies', []))}")
            m.call(P, "get_comment_replies", {"comment_id": com[0]["id"]}, lambda d: f"{d['count']} replies")
        m.call(P, "get_post_insights", {"post_id": busiest["id"]}, lambda d: f"views={d['metrics']['post_media_view']}")
    if video:
        m.call(P, "get_post_insights", {"post_id": video["id"], "include_video": True},
               lambda d: f"video plays={d['metrics']['post_video_views']}")
    m.call(P, "top_posts", {"metric": "engagement"}, lambda d: f"{d['considered']} considered, top={d['posts'][0]['engagement']}")
    m.call(P, "top_posts", {"metric": "views", "top": 3}, lambda d: f"top views={d['posts'][0].get('views')}")
    m.call(P, "list_scheduled_posts", {}, lambda d: f"{d['count']} scheduled")
    m.call(P, "list_tagged_posts", {"limit": 5}, lambda d: f"{d['count']} tagged")
    m.call(P, "get_page_insights", {}, lambda d: f"views={d['metrics'].get('page_media_view', {}).get('total')} "
                                                  f"unavailable={d.get('unavailable')}")
    m.call(P, "get_page_insights", {"metrics": ["page_follows", "page_follows_country"], "period": "week",
                                    "since": "2026-10-01", "until": "2026-11-15"},
           lambda d: f"followers={d['metrics']['page_follows'].get('latest')}")
    m.call(P, "get_page_insights", {"metrics": ["page_impressions"]}, expect_ok=False)
    m.call(P, "insights_summary", {"period": "last_28_days"},
           lambda d: f"followers={d['followers']} posts={d['posts_published']} reach28={d['reach_rolling']['value']}")


def phase_dryrun(m: MCP, ctx: dict[str, Any]) -> None:
    P = "dryrun"
    m.call(P, "create_post", {"message": E2E_TEXT, "link": "https://treningfitness.com", "dry_run": True},
           lambda d: d["would_send"]["endpoint"])
    m.call(P, "create_photo_post", {"image_urls": [IMAGE_1, IMAGE_2], "message": E2E_TEXT,
                                    "scheduled_time": later(), "dry_run": True},
           lambda d: f"{len(d['would_send'])} requests")
    m.call(P, "create_video_post", {"video_url": SAMPLE_VIDEO, "dry_run": True}, lambda d: d["would_send"]["endpoint"])
    m.call(P, "create_post", {"message": E2E_TEXT, "scheduled_time": later(1 / 24 / 60)}, expect_ok=False)


def _delete(m: MCP, P: str, pid: str | None) -> None:
    if pid:
        m.call(P, "delete_post", {"post_id": pid}, lambda d: f"deleted={d['deleted']}")


def phase_hidden(m: MCP, ctx: dict[str, Any]) -> None:
    P = "hidden"
    t = m.call(P, "create_post", {"message": E2E_TEXT, "scheduled_time": later()}, lambda d: d["status"])
    tid = t.get("id")
    try:
        time.sleep(3)
        m.call(P, "list_scheduled_posts", {}, lambda d: f"listed={any(x['id'] == tid for x in d['posts'])}")
        if tid:
            m.call(P, "update_post", {"post_id": tid, "message": E2E_TEXT + " (edited)"}, lambda d: f"updated={d['updated']}")
    finally:
        _delete(m, P, tid)
    ph = m.call(P, "create_photo_post", {"image_urls": [IMAGE_1], "message": E2E_TEXT, "scheduled_time": later()},
                lambda d: f"{d['status']} post_id={bool(d.get('post_id'))}")
    _delete(m, P, ph.get("post_id") or ph.get("id"))
    al = m.call(P, "create_photo_post", {"image_urls": [IMAGE_1, IMAGE_2], "message": E2E_TEXT,
                                         "scheduled_time": later()}, lambda d: f"{d['status']} photos={len(d['photo_ids'])}")
    _delete(m, P, al.get("id"))
    vd = m.call(P, "create_video_post", {"video_url": SAMPLE_VIDEO, "description": E2E_TEXT, "title": "e2e",
                                         "scheduled_time": later()}, lambda d: d["status"])
    if vd.get("id"):
        time.sleep(5)
    _delete(m, P, vd.get("id"))
    m.call(P, "list_scheduled_posts", {}, lambda d: f"{d['count']} scheduled left")


def phase_comments(m: MCP, ctx: dict[str, Any]) -> None:
    P = "comments"
    posts = ctx.get("posts") or m.call(P, "list_posts", {"limit": 10}).get("posts", [])
    target = posts[0]["id"]
    c = m.call(P, "comment_on_post", {"post_id": target, "message": E2E_TEXT}, lambda d: "commented")
    cid = c.get("comment_id")
    try:
        if cid:
            r = m.call(P, "reply_to_comment", {"comment_id": cid, "message": E2E_TEXT}, lambda d: "replied")
            rid = r.get("comment_id")
            if rid:
                # Meta refuses hiding the Page's own comments: expect the explained refusal
                m.call(P, "hide_comment", {"comment_id": rid}, expect_ok=False)
                m.call(P, "delete_comment", {"comment_id": rid}, lambda d: f"deleted={d['deleted']}")
    finally:
        if cid:
            m.call(P, "delete_comment", {"comment_id": cid}, lambda d: f"deleted={d['deleted']}")


def phase_hidevisitor(m: MCP, ctx: dict[str, Any]) -> None:
    """Hide + immediately unhide ONE customer comment (approved by the user, ~2 s)."""
    P = "hidevis"
    posts = ctx.get("posts") or m.call(P, "list_posts", {"limit": 5}).get("posts", [])
    for post in posts[:5]:
        c = m.call(P, "get_comments", {"post_id": post["id"], "limit": 30, "include_replies": False},
                   lambda d: f"{d['count']} comments")
        pick = next((x for x in c.get("comments", []) if x.get("can_hide") and not x.get("is_hidden")
                     and (x.get("from") or {}).get("id") != ctx.get("page_id")), None)
        if pick:
            h = m.call(P, "hide_comment", {"comment_id": pick["id"]}, lambda d: f"hidden={d['hidden']}")
            try:
                time.sleep(1)
            finally:
                m.call(P, "unhide_comment", {"comment_id": pick["id"]}, lambda d: f"hidden={d['hidden']}")
            g = m.call(P, "get_comment", {"comment_id": pick["id"]},
                       lambda d: f"is_hidden={d['comment']['is_hidden']}")
            assert h is not None and g.get("comment", {}).get("is_hidden") is False, "comment left hidden!"
            return


def phase_publish(m: MCP, ctx: dict[str, Any]) -> None:
    P = "publish"
    p = m.call(P, "create_post", {"message": E2E_TEXT}, lambda d: f"{d['status']} link={bool(d.get('permalink_url'))}")
    try:
        if p.get("id"):
            m.call(P, "get_post", {"post_id": p["id"]}, lambda d: "read back")
    finally:
        _delete(m, P, p.get("id"))


PHASES = {"read": phase_read, "dryrun": phase_dryrun, "hidden": phase_hidden, "comments": phase_comments, "hidevisitor": phase_hidevisitor,
          "publish": phase_publish}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=".venv/bin/facebook-mcp")
    ap.add_argument("--phases", default="read,dryrun")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--approve-publish", action="store_true")
    a = ap.parse_args()
    phases = [p.strip() for p in a.phases.split(",") if p.strip()]
    if any(p in {"hidden", "comments", "hidevisitor"} for p in phases) and not a.approve:
        sys.exit("hidden/comments phases write to the Page: pass --approve")
    if "publish" in phases and not a.approve_publish:
        sys.exit("publish phase posts publicly: pass --approve-publish")
    env = dict(os.environ)
    env.setdefault("FACEBOOK_MCP_ALLOWED_PAGE_IDS", "593060474157088")
    m = MCP(a.server, env)
    print(f"{len(m.tools)} tools exposed")
    ctx: dict[str, Any] = {}
    try:
        for p in phases:
            PHASES[p](m, ctx)
    finally:
        m.close()
    for phase, name, ok, detail, secs in RESULTS:
        print(f"{'OK  ' if ok else 'FAIL'} {phase:8} {name:42} {secs:5.1f}s  {detail}")
    used = {n.split(" ")[0] for _, n, ok, _, _ in RESULTS if ok}
    missing = sorted(set(m.tools) - used)
    failed = [n for _, n, ok, _, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed; tools exercised OK: {len(used)}/{len(m.tools)}")
    if missing:
        print("not exercised:", ", ".join(missing))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
