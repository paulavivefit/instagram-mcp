"""facebook-mcp: Facebook Page tools over the Graph API.

Read
  healthcheck, list_pages, get_page
  list_posts, get_post, top_posts, list_scheduled_posts, list_tagged_posts
  get_comments, get_comment, get_comment_replies
  get_page_insights, get_post_insights, insights_summary
Write (public, on the allowed Page only)
  create_post, create_photo_post, create_video_post (each can schedule or dry_run)
  update_post, delete_post
  comment_on_post, reply_to_comment, hide_comment, unhide_comment, delete_comment

Every tool works only on Pages in FACEBOOK_MCP_ALLOWED_PAGE_IDS (see pages.py). The user
token comes from the same Keychain account as instagram-mcp; requests to the Page use
the Page token.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from facebook_mcp import __version__
from facebook_mcp import metrics as M
from facebook_mcp import pages as P
from instagram_mcp import audit, auth, graph_client
from instagram_mcp import insights_shape as SH
from instagram_mcp import validators as V

mcp = FastMCP("facebook-mcp")

# ------------------------------ parameter types ------------------------------ #
PageIdArg = Annotated[
    str | None,
    Field(description="Facebook Page id (from list_pages). Omit to use the connector's Page."),
]
PostIdArg = Annotated[
    str,
    Field(description="Post id from list_posts / get_post (looks like <page_id>_<number>), or a photo/video id."),
]
CommentIdArg = Annotated[
    str, Field(description="Comment id from get_comments or get_comment_replies (looks like 123_456).")
]
AfterArg = Annotated[
    str | None,
    Field(description="Paging cursor: pass next_cursor from a previous result to continue. Omit to start at the newest."),
]
SinceArg = Annotated[str | None, Field(description="Start date, e.g. 2026-11-01 (inclusive). Optional.")]
UntilArg = Annotated[str | None, Field(description="End date, e.g. 2026-11-15 (inclusive). Optional.")]
MessageArg = Annotated[str, Field(description="Text of the post or comment (plain text, emoji allowed).")]
ScheduleArg = Annotated[
    str | None,
    Field(
        description="Publish later instead of now: ISO date-time with timezone, e.g. 2026-11-20T18:00:00+01:00. "
        "Must be 10 minutes to 30 days ahead. Omit to publish immediately."
    ),
]
DryRunArg = Annotated[
    bool,
    Field(description="True to validate and return exactly what would be sent, without posting anything."),
]
CompactArg = Annotated[
    bool,
    Field(description="True for analysing many posts: counts, date, link and the first 150 characters of text only."),
]

# ------------------------------- field lists -------------------------------- #
PAGE_FIELDS = (
    "id,name,username,category,about,link,website,fan_count,followers_count,"
    "verification_status,picture{url},instagram_business_account{id,username}"
)
COUNT_FIELDS = "shares,reactions.summary(true).limit(0),comments.summary(true).limit(0)"
POST_FIELDS = (
    "id,message,created_time,updated_time,permalink_url,status_type,is_published,full_picture,"
    "attachments{media_type,type,url,title,unshimmed_url,subattachments.limit(10){media_type,type,url}},"
    + COUNT_FIELDS
)
COMPACT_POST_FIELDS = "id,message,created_time,permalink_url,status_type," + COUNT_FIELDS
COMMENT_FIELDS = "id,message,created_time,from,like_count,comment_count,is_hidden,can_hide,can_remove,attachment{type,url}"
REPLY_PAGE = 25
COMMENT_WITH_REPLIES = COMMENT_FIELDS + f",comments.limit({REPLY_PAGE}){{{COMMENT_FIELDS}}}"
TAGGED_FIELDS = "id,message,created_time,from,permalink_url"
SCHEDULED_FIELDS = "id,message,scheduled_publish_time,created_time,full_picture"
COMPACT_CHARS = 150
MESSAGE_MAX = 63206
CEIL_POSTS, CEIL_COMMENTS = 2000, 5000
TOP_POSTS_MAX = 300
TOP_POSTS_INSIGHT_CAP = 60
INSIGHTS_MAX_DAYS = 90
SCHEDULE_MIN = timedelta(minutes=10)
SCHEDULE_MAX = timedelta(days=30)
USAGE_REFUSE_PERCENT = 80

REQUIRED_PERMISSIONS = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "read_insights",
)
AUTHOR_HIDDEN_NOTE = (
    "Meta hides commenter names/ids in development mode (until App Review); comment text is complete."
)


# --------------------------------- plumbing --------------------------------- #
def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _summarize(result: Any) -> Any:
    if isinstance(result, dict):
        out = {"ok": result.get("ok")}
        for k in ("count", "page_id", "id", "post_id", "comment_id", "dry_run"):
            if k in result:
                out[k] = result[k]
        return out
    return {"type": type(result).__name__}


def _guard(tool: str, payload: dict[str, Any], impl: Any) -> dict[str, Any]:
    """Run a tool body with audit + sanitized error shaping. Never raises."""
    start = time.perf_counter()
    safe_in = audit.sanitize_payload(payload)
    try:
        result = impl()
        audit.record(f"fb.{tool}", execution_time_ms=_ms(start), io={"input": safe_in, "output": _summarize(result)})
        return result
    except Exception as exc:  # noqa: BLE001 - boundary
        error_class = audit.classify_error(exc)
        message = audit.sanitize_error(str(exc))
        audit.record(
            f"fb.{tool}",
            execution_time_ms=_ms(start),
            io={"input": safe_in, "output": {"error": message}},
            error_class=error_class,
        )
        if error_class == "oauth":
            P.clear_cache()
            auth.clear_cache()
        out: dict[str, Any] = {"ok": False, "error": message, "error_class": error_class}
        for attr in ("code", "subcode"):
            if isinstance(getattr(exc, attr, None), int):
                out[attr] = getattr(exc, attr)
        extra = getattr(exc, "extra", None)
        if isinstance(extra, dict):
            safe = audit.sanitize_payload({k: v for k, v in extra.items() if k != "uncertain"})
            out.update({k: v for k, v in safe.items() if k not in out})
        return out


def _check_budget(limit: Any) -> None:
    if not (isinstance(limit, str) and limit.strip().lower() == "all"):
        return
    pct = graph_client.LAST_USAGE.get("max_percent")
    if isinstance(pct, (int, float)) and pct >= USAGE_REFUSE_PERCENT:
        raise graph_client.GraphAPIError(
            f'Meta reports {pct}% of the hourly API quota used; a limit="all" walk would likely be cut off.',
            error_class="rate_limited",
            extra={"hint": "Use a smaller limit or since/until, or try again within the hour."},
        )


def _paged(out: dict[str, Any], items: list[Any], cursor: str | None, key: str) -> dict[str, Any]:
    out.update({"count": len(items), key: items})
    if cursor:
        out["next_cursor"] = cursor
        out["truncated"] = True
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _message(value: str | None, *, field: str = "message", required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise V.ValidationError(f"{field} must not be empty")
        return None
    if not isinstance(value, str):
        raise V.ValidationError(f"{field} must be text")
    if len(value) > MESSAGE_MAX:
        raise V.ValidationError(f"{field} is {len(value)} characters; Facebook allows up to {MESSAGE_MAX}")
    return value


def _link(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip().lower().startswith(("https://", "http://")):
        raise V.ValidationError("link must be an http(s):// URL")
    return value.strip()


def _schedule(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) <= 10:
        raise V.ValidationError(
            "scheduled_time needs a date AND time with timezone, e.g. 2026-11-20T18:00:00+01:00"
        )
    when = V.validate_date(value, field="scheduled_time")
    delta = when - _now()
    if delta < SCHEDULE_MIN:
        raise V.ValidationError("scheduled_time must be at least 10 minutes in the future")
    if delta > SCHEDULE_MAX:
        raise V.ValidationError("scheduled_time can be at most 30 days ahead")
    return when


def _range(since: str | None, until: str | None, *, default_days: int, max_days: int | None):
    """(since_dt, until_dt) as UTC datetimes; until is exclusive (end of the until day)."""
    s = V.validate_date(since, field="since")
    u = V.validate_date(until, field="until", end_of_day=True)
    if u is None:
        u = _now()
    if s is None:
        s = u - timedelta(days=default_days)
    if s >= u:
        raise V.ValidationError("since must be before until")
    if max_days is not None and (u - s) > timedelta(days=max_days + 1):
        raise V.ValidationError(f"date range can be at most {max_days} days (Meta limit); split it")
    return s, u


def _flat_post(item: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    out = dict(item)
    reactions = out.pop("reactions", None)
    comments = out.pop("comments", None)
    shares = out.pop("shares", None)
    out["reactions_count"] = ((reactions or {}).get("summary") or {}).get("total_count", 0)
    out["comments_count"] = ((comments or {}).get("summary") or {}).get("total_count", 0)
    out["shares_count"] = (shares or {}).get("count", 0)
    out["engagement"] = out["reactions_count"] + out["comments_count"] + out["shares_count"]
    if isinstance(out.get("attachments"), dict):
        out["attachments"] = out["attachments"].get("data") or []
    if compact:
        msg = out.get("message")
        if isinstance(msg, str) and len(msg) > COMPACT_CHARS:
            out["message"] = msg[:COMPACT_CHARS] + "…"
            out["message_truncated"] = True
    return out


def _flat_comment(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    replies = out.pop("comments", None)
    if replies is not None:
        out["replies"] = [dict(r) for r in (replies.get("data") or [])]
        if (replies.get("paging") or {}).get("next"):
            out["more_replies"] = True
    return out


def _author_note(out: dict[str, Any], comments: list[dict[str, Any]]) -> None:
    flat = comments + [r for c in comments for r in c.get("replies", [])]
    if any("from" not in c for c in flat):
        out["note"] = AUTHOR_HIDDEN_NOTE


def _ok(page: P.Page, **fields: Any) -> dict[str, Any]:
    return {"ok": True, **page.who(), **fields}


def _iso(epoch: Any) -> Any:
    if isinstance(epoch, (int, float)):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    return epoch


# ================================== tools =================================== #
RO = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
DESTROY = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


@mcp.tool(annotations={"title": "Check the connection", **RO})
def healthcheck() -> dict[str, Any]:
    """Check the token, its permissions and which Facebook Page(s) this connector may use."""

    def _impl():
        acct = auth.resolve(None)
        user = acct.client()
        me = user.get("me", fields="id,name")
        perms = user.get("me/permissions").get("data") or []
        granted = sorted(p["permission"] for p in perms if p.get("status") == "granted")
        pages = P.allowed_pages()
        missing = [p for p in REQUIRED_PERMISSIONS if p not in granted]
        out = {
            "ok": True,
            "server_version": __version__,
            "graph_version": graph_client.GRAPH_VERSION,
            "account": acct.label,
            "user": me.get("name"),
            "allowed_pages": [{"page_id": p.id, "page_name": p.name} for p in pages.values()],
            "permissions": granted,
            "missing_permissions": missing,
            "page_lock": P.ALLOWED_ENV + (" (set)" if P.allowed_ids_from_env() is not None
                                          else " not set: only the Page linked to Instagram"),
        }
        if missing:
            out["hint"] = "Regenerate the token in Graph API Explorer with these permissions ticked: " + ", ".join(
                missing)
        return out

    return _guard("healthcheck", {}, _impl)


@mcp.tool(annotations={"title": "List allowed Pages", **RO})
def list_pages() -> dict[str, Any]:
    """List the Facebook Pages this connector may use (others on the token are hidden)."""

    def _impl():
        items = []
        for p in P.allowed_pages().values():
            info = p.client.get(p.id, fields="followers_count,fan_count,link")
            items.append({"page_id": p.id, "page_name": p.name, "tasks": list(p.tasks), **info})
        return _paged({"ok": True}, items, None, "pages")

    return _guard("list_pages", {}, _impl)


@mcp.tool(annotations={"title": "Get Page profile", **RO})
def get_page(page_id: PageIdArg = None) -> dict[str, Any]:
    """Get the Page profile: name, category, about, website, followers and linked Instagram."""

    def _impl():
        page = P.resolve(page_id)
        return _ok(page, profile=page.client.get(page.id, fields=PAGE_FIELDS))

    return _guard("get_page", {"page_id": page_id}, _impl)


@mcp.tool(annotations={"title": "List Page posts", **RO})
def list_posts(
    page_id: PageIdArg = None,
    since: SinceArg = None,
    until: UntilArg = None,
    limit: Annotated[
        int | str, Field(description='How many posts, newest first: 1-100, or "all" (up to 2000).')
    ] = 25,
    after: AfterArg = None,
    compact: CompactArg = False,
) -> dict[str, Any]:
    """List the Page's own posts (newest first) with reactions, comments and shares counts."""

    def _impl():
        page = P.resolve(page_id)
        _check_budget(limit)
        n = V.validate_limit(limit, ceiling=100, all_ceiling=CEIL_POSTS)
        cursor = V.validate_cursor(after)
        s = V.validate_date(since, field="since")
        u = V.validate_date(until, field="until", end_of_day=True)
        if s and u and s >= u:
            raise V.ValidationError("since must be before until")
        items, nxt = page.client.paginate(
            f"{page.id}/posts",
            max_items=n,
            after=cursor,
            page_size=100 if not compact else 100,
            fields=COMPACT_POST_FIELDS if compact else POST_FIELDS,
            since=_epoch(s) if s else None,
            until=_epoch(u) if u else None,
        )
        posts = [_flat_post(i, compact=compact) for i in items]
        return _paged(_ok(page), posts, nxt, "posts")

    return _guard("list_posts", {"page_id": page_id, "since": since, "until": until, "limit": limit,
                                 "after": bool(after), "compact": compact}, _impl)


@mcp.tool(annotations={"title": "Get one post", **RO})
def get_post(post_id: PostIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Get one post of the Page: full text, attachments, link and counts."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        return _ok(page, post=_flat_post(page.client.get(pid, fields=POST_FIELDS)))

    return _guard("get_post", {"post_id": post_id}, _impl)


@mcp.tool(annotations={"title": "Top posts", **RO})
def top_posts(
    page_id: PageIdArg = None,
    since: SinceArg = None,
    until: UntilArg = None,
    metric: Annotated[
        Literal["engagement", "reactions", "comments", "shares", "views", "reach", "clicks"],
        Field(description="Rank by: engagement (reactions+comments+shares), reactions, comments, shares, "
                          "views, reach or clicks (the last three need one stats call per post)."),
    ] = "engagement",
    top: Annotated[int, Field(description="How many top posts to return (1-50).")] = 10,
) -> dict[str, Any]:
    """Rank the Page's posts in a date range (default last 30 days) by engagement or stats."""

    def _impl():
        page = P.resolve(page_id)
        k = V.validate_limit(top, field="top", default=10, ceiling=50)
        s, u = _range(since, until, default_days=30, max_days=None)
        items, nxt = page.client.paginate(
            f"{page.id}/posts", max_items=TOP_POSTS_MAX, fields=COMPACT_POST_FIELDS,
            since=_epoch(s), until=_epoch(u),
        )
        posts = [_flat_post(i, compact=True) for i in items]
        out = _ok(page, since=s.date().isoformat(), until=u.date().isoformat(), metric=metric,
                  considered=len(posts))
        if nxt:
            out["note_more_posts"] = f"Only the newest {TOP_POSTS_MAX} posts in the range were considered."
        key = {"engagement": "engagement", "reactions": "reactions_count", "comments": "comments_count",
               "shares": "shares_count"}.get(metric)
        if key is None:
            stat = {"views": "post_media_view", "reach": "post_total_media_view_unique", "clicks": "post_clicks"}[
                metric]
            posts.sort(key=lambda p: p["engagement"], reverse=True)
            if len(posts) > TOP_POSTS_INSIGHT_CAP:
                out["note_insight_cap"] = (f"{metric} was fetched for the {TOP_POSTS_INSIGHT_CAP} posts with the "
                                           "highest engagement only (one API call per post).")
                posts = posts[:TOP_POSTS_INSIGHT_CAP]
            for p in posts:
                rows = page.client.get(f"{p['id']}/insights", metric=stat).get("data") or []
                p[metric] = SH.row_value(rows[0]) if rows else None
            key = metric
        posts.sort(key=lambda p: (p.get(key) is not None, p.get(key) or 0), reverse=True)
        return _paged(out, posts[:k], None, "posts")

    return _guard("top_posts", {"since": since, "until": until, "metric": metric, "top": top}, _impl)


@mcp.tool(annotations={"title": "List scheduled posts", **RO})
def list_scheduled_posts(page_id: PageIdArg = None) -> dict[str, Any]:
    """List posts scheduled for later publication on the Page."""

    def _impl():
        page = P.resolve(page_id)
        items, nxt = page.client.paginate(f"{page.id}/scheduled_posts", max_items=200, fields=SCHEDULED_FIELDS)
        for i in items:
            if "scheduled_publish_time" in i:
                i["scheduled_publish_time"] = _iso(i["scheduled_publish_time"])
        return _paged(_ok(page), items, nxt, "posts")

    return _guard("list_scheduled_posts", {}, _impl)


@mcp.tool(annotations={"title": "Posts that tag the Page", **RO})
def list_tagged_posts(
    page_id: PageIdArg = None,
    limit: Annotated[int, Field(description="How many posts, newest first (1-100).")] = 25,
    after: AfterArg = None,
) -> dict[str, Any]:
    """List posts by other people or Pages that tag this Page (newest first)."""

    def _impl():
        page = P.resolve(page_id)
        n = V.validate_limit(limit, ceiling=100)
        items, nxt = page.client.paginate(f"{page.id}/tagged", max_items=n, after=V.validate_cursor(after),
                                          fields=TAGGED_FIELDS)
        out = _paged(_ok(page), items, nxt, "posts")
        if any("from" not in i for i in items):
            out["note"] = "Meta hides some authors in development mode (until App Review)."
        return out

    return _guard("list_tagged_posts", {"limit": limit, "after": bool(after)}, _impl)


@mcp.tool(annotations={"title": "Get post comments", **RO})
def get_comments(
    post_id: PostIdArg,
    page_id: PageIdArg = None,
    limit: Annotated[
        int | str, Field(description='How many top-level comments: 1-100, or "all" (up to 5000).')
    ] = 50,
    after: AfterArg = None,
    order: Annotated[
        Literal["newest", "oldest"], Field(description="newest first (default) or oldest first.")
    ] = "newest",
    include_replies: Annotated[
        bool, Field(description="True to include up to 25 replies under each comment (more via get_comment_replies).")
    ] = True,
) -> dict[str, Any]:
    """Get comments on one of the Page's posts, with replies, hidden state and moderation flags."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        _check_budget(limit)
        n = V.validate_limit(limit, default=50, ceiling=100, all_ceiling=CEIL_COMMENTS)
        items, nxt = page.client.paginate(
            f"{pid}/comments", max_items=n, after=V.validate_cursor(after),
            fields=COMMENT_WITH_REPLIES if include_replies else COMMENT_FIELDS,
            filter="toplevel",
            order="reverse_chronological" if order == "newest" else "chronological",
        )
        comments = [_flat_comment(i) for i in items]
        out = _paged(_ok(page, post_id=pid), comments, nxt, "comments")
        _author_note(out, comments)
        return out

    return _guard("get_comments", {"post_id": post_id, "limit": limit, "order": order}, _impl)


@mcp.tool(annotations={"title": "Get one comment", **RO})
def get_comment(comment_id: CommentIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Get one comment on the Page's content, with its first replies."""

    def _impl():
        page = P.resolve(page_id)
        cid = P.assert_own_comment(page, comment_id)
        c = _flat_comment(page.client.get(cid, fields=COMMENT_WITH_REPLIES))
        out = _ok(page, comment=c)
        _author_note(out, [c])
        return out

    return _guard("get_comment", {"comment_id": comment_id}, _impl)


@mcp.tool(annotations={"title": "Get comment replies", **RO})
def get_comment_replies(
    comment_id: CommentIdArg,
    page_id: PageIdArg = None,
    limit: Annotated[int, Field(description="How many replies, oldest first (1-100).")] = 50,
    after: AfterArg = None,
) -> dict[str, Any]:
    """Get the replies under one comment (oldest first)."""

    def _impl():
        page = P.resolve(page_id)
        cid = P.assert_own_comment(page, comment_id)
        n = V.validate_limit(limit, default=50, ceiling=100)
        items, nxt = page.client.paginate(f"{cid}/comments", max_items=n, after=V.validate_cursor(after),
                                          fields=COMMENT_FIELDS, order="chronological")
        out = _paged(_ok(page, comment_id=cid), items, nxt, "replies")
        _author_note(out, items)
        return out

    return _guard("get_comment_replies", {"comment_id": comment_id, "limit": limit}, _impl)


# --------------------------------- insights --------------------------------- #
def _validate_metrics(values: list[str] | None, allowed: dict[str, str], default: tuple[str, ...]) -> list[str]:
    if not values:
        return list(default)
    if not isinstance(values, list) or len(values) > 20:
        raise V.ValidationError("metrics must be a list of up to 20 metric names")
    out = []
    for m in values:
        name = str(m).strip().lower()
        if name not in allowed:
            hint = f" {M.RETIRED_HINT}" if "impression" in name or "fan" in name or "engaged" in name else ""
            raise V.ValidationError(f"unknown metric {V.truncate(name, 50)!r}. Valid: {', '.join(allowed)}.{hint}")
        if name not in out:
            out.append(name)
    return out


@mcp.tool(annotations={"title": "Page stats", **RO})
def get_page_insights(
    page_id: PageIdArg = None,
    metrics: Annotated[
        list[str] | None,
        Field(description="Metric names (default: views, reach, engagements, new followers, unfollows, "
                          "profile visits, video views). Valid: " + ", ".join(
                              list(M.PAGE_METRICS) + list(M.PAGE_BREAKDOWN_METRICS)) + "."),
    ] = None,
    period: Annotated[
        Literal["day", "week", "days_28"],
        Field(description="day = one value per day; week / days_28 = rolling 7- or 28-day totals per day."),
    ] = "day",
    since: SinceArg = None,
    until: UntilArg = None,
) -> dict[str, Any]:
    """Get Page stats (views, reach, engagement, followers, video) per day for a date range (default last 7 days, max 90)."""

    def _impl():
        page = P.resolve(page_id)
        allowed = {**M.PAGE_METRICS, **M.PAGE_BREAKDOWN_METRICS}
        names = _validate_metrics(metrics, allowed, M.DEFAULT_PAGE_METRICS)
        s, u = _range(since, until, default_days=7, max_days=INSIGHTS_MAX_DAYS)
        series = [n for n in names if n not in M.PAGE_BREAKDOWN_METRICS]
        breakdown = [n for n in names if n in M.PAGE_BREAKDOWN_METRICS]
        result: dict[str, Any] = {}
        if series:
            rows = page.client.get(f"{page.id}/insights", metric=",".join(series), period=period,
                                   since=_epoch(s), until=_epoch(u)).get("data") or []
            for row in rows:
                vals = SH.flatten_series(row.get("values"))
                entry: dict[str, Any] = {"period": row.get("period"), "series": vals}
                nums = [v["value"] for v in vals if isinstance(v["value"], (int, float))]
                if period == "day" and row.get("name") in M.SUMMABLE and nums:
                    entry["total"] = sum(nums)
                elif nums:
                    entry["latest"] = nums[-1]
                result[row["name"]] = entry
        for name in breakdown:
            rows = page.client.get(f"{page.id}/insights", metric=name, period="day").get("data") or []
            vals = (rows[0].get("values") or []) if rows else []
            latest = vals[-1].get("value") if vals else None
            if isinstance(latest, dict):
                latest = dict(sorted(latest.items(), key=lambda kv: kv[1], reverse=True))
            result[name] = {"period": "day", "latest": latest}
        out = _ok(page, since=s.date().isoformat(), until=u.date().isoformat(), period=period, metrics=result)
        missing = [n for n in names if n not in result]
        if missing:
            out["unavailable"] = missing
        out["dates_note"] = "Dates are Meta's Pacific-time days."
        return out

    return _guard("get_page_insights", {"metrics": metrics, "period": period, "since": since, "until": until},
                  _impl)


@mcp.tool(annotations={"title": "Post stats", **RO})
def get_post_insights(
    post_id: PostIdArg,
    page_id: PageIdArg = None,
    include_video: Annotated[
        bool, Field(description="True to add video metrics (plays, watch time, retention); 0 for non-video posts.")
    ] = False,
) -> dict[str, Any]:
    """Get lifetime stats of one post: views, reach, clicks, reactions by type, and optionally video metrics."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        names = list(M.DEFAULT_POST_METRICS) + (list(M.POST_VIDEO_METRICS) if include_video else [])
        rows = page.client.get(f"{pid}/insights", metric=",".join(names)).get("data") or []
        values = SH.media_metrics(rows)
        return _ok(page, post_id=pid, metrics={n: values.get(n) for n in names})

    return _guard("get_post_insights", {"post_id": post_id, "include_video": include_video}, _impl)


@mcp.tool(annotations={"title": "Stats summary", **RO})
def insights_summary(
    page_id: PageIdArg = None,
    period: Annotated[
        Literal["last_7_days", "last_28_days", "last_90_days"], Field(description="Time window to summarise.")
    ] = "last_28_days",
) -> dict[str, Any]:
    """One-call overview: followers, totals for views, reach, engagement, follows/unfollows, visits, video, and top 5 posts."""

    def _impl():
        page = P.resolve(page_id)
        days = {"last_7_days": 7, "last_28_days": 28, "last_90_days": 90}[period]
        u = _now()
        s = u - timedelta(days=days)
        info = page.client.get(page.id, fields="followers_count,fan_count")
        summable = [m for m in M.DEFAULT_PAGE_METRICS if m in M.SUMMABLE]
        rows = page.client.get(f"{page.id}/insights", metric=",".join(summable), period="day",
                               since=_epoch(s), until=_epoch(u)).get("data") or []
        totals = {r["name"]: sum(v.get("value") or 0 for v in (r.get("values") or [])
                                 if isinstance(v.get("value"), (int, float))) for r in rows}
        reach_rows = page.client.get(f"{page.id}/insights", metric="page_total_media_view_unique",
                                     period="days_28" if days >= 28 else "week",
                                     since=_epoch(u - timedelta(days=2)), until=_epoch(u)).get("data") or []
        reach_vals = (reach_rows[0].get("values") or []) if reach_rows else []
        items, nxt = page.client.paginate(f"{page.id}/posts", max_items=TOP_POSTS_MAX, fields=COMPACT_POST_FIELDS,
                                          since=_epoch(s), until=_epoch(u))
        posts = sorted((_flat_post(i, compact=True) for i in items), key=lambda p: p["engagement"], reverse=True)
        out = _ok(
            page,
            period=period,
            since=s.date().isoformat(),
            until=u.date().isoformat(),
            followers=info.get("followers_count"),
            totals=totals,
            reach_rolling={
                "window": "28 days" if days >= 28 else "7 days",
                "value": reach_vals[-1].get("value") if reach_vals else None,
            },
            posts_published=len(posts),
            post_engagement_total=sum(p["engagement"] for p in posts),
            top_posts=posts[:5],
        )
        if nxt:
            out["note"] = f"More than {TOP_POSTS_MAX} posts in the window; post totals cover the newest only."
        return out

    return _guard("insights_summary", {"period": period}, _impl)


# --------------------------------- publishing -------------------------------- #
def _sched_fields(when: datetime | None) -> dict[str, Any]:
    if when is None:
        return {}
    return {"published": "false", "scheduled_publish_time": str(_epoch(when))}


def _dry(page: P.Page, endpoint: str, fields: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return _ok(page, dry_run=True, would_send={"method": "POST", "endpoint": endpoint, "fields": fields}, **extra)


def _publish_result(page: P.Page, res: dict[str, Any], when: datetime | None, *, id_key: str = "id") -> dict:
    out = _ok(page, id=res.get(id_key) or res.get("id"))
    if res.get("post_id"):
        out["post_id"] = res["post_id"]
    if when is not None:
        out["scheduled_for"] = when.isoformat()
        out["status"] = "scheduled"
    else:
        out["status"] = "published"
        target = res.get("post_id") or res.get("id")
        with contextlib.suppress(graph_client.GraphAPIError):
            out["permalink_url"] = page.client.get(target, fields="permalink_url").get("permalink_url")
    return out


@mcp.tool(annotations={"title": "Publish a text/link post", **WRITE})
def create_post(
    message: MessageArg,
    link: Annotated[str | None, Field(description="Optional URL to attach as a link preview.")] = None,
    scheduled_time: ScheduleArg = None,
    page_id: PageIdArg = None,
    dry_run: DryRunArg = False,
) -> dict[str, Any]:
    """Publish (or schedule) a text post, optionally with a link preview, on the Page. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        when = _schedule(scheduled_time)
        fields = {"message": _message(message), "link": _link(link), **_sched_fields(when)}
        fields = {k: v for k, v in fields.items() if v is not None}
        if dry_run:
            return _dry(page, f"{page.id}/feed", fields)
        return _publish_result(page, page.client.post(f"{page.id}/feed", **fields), when)

    return _guard("create_post", {"message": V.truncate(message, 80), "link": link,
                                  "scheduled_time": scheduled_time, "dry_run": dry_run}, _impl)


PHOTO_HINT = (
    "Facebook could not download this image. Use a direct public https link to a JPG/PNG file "
    "(not a web page or a thumbnail service that blocks bots), e.g. a file on treningfitness.com."
)


def _photo_call(page: P.Page, fields: dict[str, Any]) -> dict[str, Any]:
    try:
        return page.client.post(f"{page.id}/photos", **fields)
    except graph_client.GraphAPIError as exc:
        if "image" in str(exc).lower() and "hint" not in exc.extra:
            exc.extra["hint"] = PHOTO_HINT
            exc.extra["image_url"] = fields.get("url")
        raise


def _upload_photo(page: P.Page, url: str, upload: dict[str, Any]) -> str:
    return _photo_call(page, {"url": url, **upload})["id"]


@mcp.tool(annotations={"title": "Publish a photo post", **WRITE})
def create_photo_post(
    image_urls: Annotated[
        list[str], Field(description="1-10 public https links to JPG/PNG images (several = one album post).")
    ],
    message: Annotated[str | None, Field(description="Text of the post (optional).")] = None,
    scheduled_time: ScheduleArg = None,
    page_id: PageIdArg = None,
    dry_run: DryRunArg = False,
) -> dict[str, Any]:
    """Publish (or schedule) a post with 1-10 photos on the Page. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        if not isinstance(image_urls, list) or not 1 <= len(image_urls) <= 10:
            raise V.ValidationError("image_urls must be a list of 1-10 links")
        urls = [V.validate_public_https_url(u, field="image_urls") for u in image_urls]
        text = _message(message, required=False)
        when = _schedule(scheduled_time)
        if len(urls) == 1:
            fields = {"url": urls[0], "caption": text, **_sched_fields(when)}
            if when is not None:
                fields["unpublished_content_type"] = "SCHEDULED"
            fields = {k: v for k, v in fields.items() if v is not None}
            if dry_run:
                return _dry(page, f"{page.id}/photos", fields)
            return _publish_result(page, _photo_call(page, fields), when)
        upload = {"published": "false", **({"temporary": "true"} if when is not None else {})}
        feed = {"message": text, **_sched_fields(when)}
        feed = {k: v for k, v in feed.items() if v is not None}
        if dry_run:
            return _ok(page, dry_run=True, would_send=[
                *({"method": "POST", "endpoint": f"{page.id}/photos", "fields": {"url": u, **upload}} for u in urls),
                {"method": "POST", "endpoint": f"{page.id}/feed",
                 "fields": {**feed, "attached_media": f"<{len(urls)} uploaded photo ids>"}},
            ])
        ids: list[str] = []
        try:
            for u in urls:
                ids.append(_upload_photo(page, u, upload))
        except Exception:
            for fbid in ids:  # don't leave unpublished uploads behind
                with contextlib.suppress(graph_client.GraphAPIError):
                    page.client.delete(fbid)
            raise
        for i, fbid in enumerate(ids):
            feed[f"attached_media[{i}]"] = json.dumps({"media_fbid": fbid})
        out = _publish_result(page, page.client.post(f"{page.id}/feed", **feed), when)
        out["photo_ids"] = ids
        return out

    return _guard("create_photo_post", {"image_urls": image_urls, "message": V.truncate(message or "", 80),
                                        "scheduled_time": scheduled_time, "dry_run": dry_run}, _impl)


@mcp.tool(annotations={"title": "Publish a video post", **WRITE})
def create_video_post(
    video_url: Annotated[str, Field(description="Public https link to an MP4 file (H.264 video, AAC audio).")],
    description: Annotated[str | None, Field(description="Text shown with the video (optional).")] = None,
    title: Annotated[str | None, Field(description="Video title (optional, max 255 characters).")] = None,
    scheduled_time: ScheduleArg = None,
    page_id: PageIdArg = None,
    dry_run: DryRunArg = False,
) -> dict[str, Any]:
    """Publish (or schedule) a video on the Page from a public link. Facebook processes it for a few minutes. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        url = V.validate_video_url(video_url)
        if title is not None and len(title) > 255:
            raise V.ValidationError("title can be at most 255 characters")
        when = _schedule(scheduled_time)
        fields = {"file_url": url, "description": _message(description, field="description", required=False),
                  "title": title or None, **_sched_fields(when)}
        fields = {k: v for k, v in fields.items() if v is not None}
        if dry_run:
            return _dry(page, f"{page.id}/videos", fields)
        res = page.client.post(f"{page.id}/videos", **fields)
        out = _ok(page, id=res.get("id"), status="scheduled" if when else "processing")
        if when is not None:
            out["scheduled_for"] = when.isoformat()
        out["note"] = "Facebook processes the video for a few minutes; the post id is <page_id>_<id>."
        return out

    return _guard("create_video_post", {"video_url": video_url, "scheduled_time": scheduled_time,
                                        "dry_run": dry_run}, _impl)


@mcp.tool(annotations={"title": "Edit a post's text", **WRITE})
def update_post(post_id: PostIdArg, message: MessageArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Replace the text of one of the Page's posts. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        res = page.client.post(pid, message=_message(message))
        return _ok(page, post_id=pid, updated=bool(res.get("success", True)))

    return _guard("update_post", {"post_id": post_id, "message": V.truncate(message, 80)}, _impl)


@mcp.tool(annotations={"title": "Delete a post", **DESTROY})
def delete_post(post_id: PostIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Permanently delete one of the Page's posts (also scheduled posts, photos, videos). Cannot be undone."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        res = page.client.delete(pid)
        return _ok(page, post_id=pid, deleted=bool(res.get("success", True)))

    return _guard("delete_post", {"post_id": post_id}, _impl)


# ------------------------------ comment actions ------------------------------ #
@mcp.tool(annotations={"title": "Comment on a post", **WRITE})
def comment_on_post(post_id: PostIdArg, message: MessageArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Add a comment as the Page under one of the Page's posts. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        pid = P.assert_own_post(page, post_id)
        res = page.client.post(f"{pid}/comments", message=_message(message))
        return _ok(page, post_id=pid, comment_id=res.get("id"))

    return _guard("comment_on_post", {"post_id": post_id, "message": V.truncate(message, 80)}, _impl)


@mcp.tool(annotations={"title": "Reply to a comment", **WRITE})
def reply_to_comment(comment_id: CommentIdArg, message: MessageArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Reply as the Page to a comment on the Page's content. PUBLIC."""

    def _impl():
        page = P.resolve(page_id)
        cid = P.assert_own_comment(page, comment_id)
        res = page.client.post(f"{cid}/comments", message=_message(message))
        return _ok(page, comment_id=res.get("id"), reply_to=cid)

    return _guard("reply_to_comment", {"comment_id": comment_id, "message": V.truncate(message, 80)}, _impl)


HIDE_HINT = (
    "Facebook only lets a Page hide comments written by other people, not the Page's own comments "
    "(check can_hide in get_comments). Use delete_comment for the Page's own comments."
)


def _set_hidden(tool: str, comment_id: str, page_id: str | None, hidden: bool) -> dict[str, Any]:
    def _impl():
        page = P.resolve(page_id)
        cid = P.assert_own_comment(page, comment_id)
        try:
            res = page.client.post(cid, is_hidden="true" if hidden else "false")
        except graph_client.GraphAPIError as exc:
            if exc.error_class == "permission" and "hide" in str(exc).lower():
                exc.extra.setdefault("hint", HIDE_HINT)
            raise
        return _ok(page, comment_id=cid, hidden=hidden, success=bool(res.get("success", True)))

    return _guard(tool, {"comment_id": comment_id}, _impl)


@mcp.tool(annotations={"title": "Hide a comment", **WRITE})
def hide_comment(comment_id: CommentIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Hide a comment: only its author and their friends still see it. Reversible with unhide_comment."""
    return _set_hidden("hide_comment", comment_id, page_id, True)


@mcp.tool(annotations={"title": "Unhide a comment", **WRITE})
def unhide_comment(comment_id: CommentIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Make a hidden comment visible to everyone again."""
    return _set_hidden("unhide_comment", comment_id, page_id, False)


@mcp.tool(annotations={"title": "Delete a comment", **DESTROY})
def delete_comment(comment_id: CommentIdArg, page_id: PageIdArg = None) -> dict[str, Any]:
    """Permanently delete a comment on the Page's content. Cannot be undone; prefer hide_comment."""

    def _impl():
        page = P.resolve(page_id)
        cid = P.assert_own_comment(page, comment_id)
        res = page.client.delete(cid)
        return _ok(page, comment_id=cid, deleted=bool(res.get("success", True)))

    return _guard("delete_comment", {"comment_id": comment_id}, _impl)


def run() -> None:
    """Console entry point (stdio). Separate audit log from instagram-mcp."""
    os.environ["INSTAGRAM_MCP_AUDIT_PATH"] = os.environ.get("FACEBOOK_MCP_AUDIT_PATH") or os.path.expanduser(
        "~/.claude/facebook-mcp/audit.log.jsonl"
    )
    mcp.run()


if __name__ == "__main__":
    run()
