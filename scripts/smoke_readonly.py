"""Live READ-ONLY smoke test of every read tool against the configured account.

Never publishes, comments, hides, deletes or sends. Prints only ok/fail, counts and
timings: no message text, captions or tokens.

    INSTAGRAM_MCP_GRAPH_VERSION=v27.0 INSTAGRAM_MCP_DM_ENABLED=1 python scripts/smoke_readonly.py
    ... --hashtags   # also search one hashtag (uses 1 of Meta's 30 unique hashtags per 7 days)
"""

from __future__ import annotations

import sys
import time
from typing import Any

from instagram_mcp import graph_client
from instagram_mcp import server as S


def _fn(tool: Any) -> Any:
    return getattr(tool, "fn", tool)


RESULTS: list[tuple[str, bool, str, float]] = []


def check(name: str, tool: Any, *args: Any, summarize=lambda r: "", **kwargs: Any) -> dict[str, Any]:
    start = time.monotonic()
    try:
        res = _fn(tool)(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"raised {type(exc).__name__}"}
    ok = bool(res.get("ok"))
    detail = summarize(res) if ok else f"{res.get('error_class')}: {str(res.get('error'))[:90]}"
    RESULTS.append((name, ok, detail, time.monotonic() - start))
    return res


def main() -> int:
    hashtags = "--hashtags" in sys.argv
    print(f"Graph API {graph_client.GRAPH_VERSION}\n")
    check("healthcheck", S.healthcheck, summarize=lambda r: f"user={r['live_check'].get('username')}")
    check("list_accounts", S.list_accounts, summarize=lambda r: f"{len(r.get('accounts', []))} accounts")
    check("account_info", S.account_info)
    check("get_profile", S.get_profile, summarize=lambda r: f"media_count={r['profile'].get('media_count')}")
    media = check("list_media(50)", S.list_media, limit=50, summarize=lambda r: f"{r['count']} posts")
    check("list_media(all)", S.list_media, limit="all", summarize=lambda r: f"{r['count']} posts")
    check("list_media(Oct)", S.list_media, limit="all", since="2026-10-01", until="2026-10-31",
          summarize=lambda r: f"{r['count']} posts")
    posts = media.get("media", []) if media.get("ok") else []
    carousel = next((m for m in posts if m.get("media_type") == "CAROUSEL_ALBUM"), None)
    reel = next((m for m in posts if m.get("media_product_type") == "REELS"), None)
    busiest = max(posts, key=lambda m: m.get("comments_count", 0), default=None)
    if carousel:
        check("get_media(carousel)", S.get_media, carousel["id"],
              summarize=lambda r: f"{len((r['media'].get('children') or {}).get('data', []))} children")
        check("get_media_insights(feed)", S.get_media_insights, carousel["id"],
              summarize=lambda r: f"{len(r['insights'])} metrics")
    if reel:
        check("get_media_insights(reel)", S.get_media_insights, reel["id"],
              summarize=lambda r: f"{len(r['insights'])} metrics")
    if busiest:
        com = check("get_comments(all+replies)", S.get_comments, busiest["id"], limit="all",
                    include_replies=True, summarize=lambda r: f"{r['count']} + {r.get('reply_count')} replies")
        parent = next((c for c in com.get("comments", []) if c.get("replies")), None)
        if parent:
            check("get_comment_replies", S.get_comment_replies, parent["id"], limit="all",
                  summarize=lambda r: f"{r['count']} replies")
    check("get_account_insights", S.get_account_insights,
          summarize=lambda r: f"{len(r['insights'])} metrics, errors={len(r.get('errors', []))}")
    check("get_account_insights(range)", S.get_account_insights, since="2026-08-01", until="2026-10-31",
          summarize=lambda r: f"{len(r['insights'])} metrics, errors={len(r.get('errors', []))}")
    check("get_audience_insights", S.get_audience_insights,
          summarize=lambda r: f"{sorted(r['breakdowns'])}, errors={len(r.get('errors', []))}")
    check("insights_summary", S.insights_summary, summarize=lambda r: f"followers={r.get('followers_now')}")
    check("top_posts", S.top_posts, n=3, summarize=lambda r: f"{r['posts_considered']} considered")
    check("list_stories", S.list_stories, include_insights=True, summarize=lambda r: f"{r['count']} active")
    check("get_mentions(all)", S.get_mentions, limit="all", summarize=lambda r: f"{r['count']} tagged")
    check("business_discovery", S.business_discovery, "paula_treningfitness", with_media=True,
          media_limit=150, summarize=lambda r: f"{len(r['business_discovery']['media']['data'])} posts")
    check("publishing_limit", S.publishing_limit, summarize=lambda r: f"{r['limit']}")
    if hashtags:
        found = check("search_hashtag", S.search_hashtag, "fitness",
                      summarize=lambda r: f"{len(r['results'])} ids")
        if found.get("results"):
            check("get_hashtag_media", S.get_hashtag_media, found["results"][0]["id"], limit=60,
                  summarize=lambda r: f"{r['count']} posts")
    conv = check("list_conversations", S.list_conversations, limit=3, summarize=lambda r: f"{r['count']} convos")
    if conv.get("conversations"):
        check("get_messages", S.get_messages, conv["conversations"][0]["id"], limit=10,
              summarize=lambda r: f"{len(r['messages'])} msgs")

    width = max(len(n) for n, *_ in RESULTS)
    for name, ok, detail, secs in RESULTS:
        print(f"{'OK  ' if ok else 'FAIL'} {name:<{width}}  {secs:5.1f}s  {detail}")
    failed = [n for n, ok, *_ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
