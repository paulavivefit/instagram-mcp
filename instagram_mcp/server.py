"""instagram-mcp v0.1.2 — official Instagram Graph API MCP.

A self-contained, multi-account wrapper over the OFFICIAL Instagram Graph API
(graph.facebook.com). Read, publish, comment, and pull analytics for any
Instagram Professional (Business/Creator) account. ToS-safe — no private API.

Tool surface (29):

  HEALTH + ACCOUNTS (6)  healthcheck, list_accounts, add_account,
                         set_default_account, remove_account, account_info
  PROFILE + MEDIA (3)    get_profile, list_media, get_media
  INSIGHTS (3)           get_account_insights, get_media_insights,
                         get_audience_insights
  PUBLISHING (6)         publish_image, publish_video, publish_reel,
                         publish_carousel, publish_story, publishing_limit
  COMMENTS (4)           get_comments, reply_to_comment, hide_comment,
                         delete_comment
  DISCOVERY (4)          search_hashtag, get_hashtag_media, get_mentions,
                         business_discovery
  DIRECT MESSAGES (3)    list_conversations, get_messages, send_message
                         (gated behind Meta App Review — see _require_dm)

Safety model:
  - Egress pinned to the Meta host allow-list (graph_client.ALLOWED_HOSTS).
  - sanitize_error() on every result + error before the seam (tokens never leak).
  - 4-field observability JSONL on every call (audit.py).
  - INPUT rail validation (validators.py) before any Graph call.
  - Tokens live in the OS keychain (auth.py); never an argument echoed back.

Multi-account: pass account="<label>" to any tool, or omit to use the default.
Configure with add_account(), or set INSTAGRAM_MCP_ACCESS_TOKEN +
INSTAGRAM_MCP_IG_USER_ID for a single zero-config account labelled "env".
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastmcp import FastMCP

from instagram_mcp import audit, auth, graph_client
from instagram_mcp import insights_metrics as IM
from instagram_mcp import validators as V

mcp = FastMCP("instagram-mcp")

# ----------------------------- field selections ----------------------------- #
PROFILE_FIELDS = ("id,username,name,biography,followers_count,follows_count,"
                  "media_count,profile_picture_url,website")
MEDIA_FIELDS = ("id,caption,media_type,media_product_type,media_url,permalink,"
                "thumbnail_url,timestamp,username,like_count,comments_count")
COMMENT_FIELDS = "id,text,username,timestamp,like_count,hidden"
COMMENT_WITH_REPLIES_FIELDS = COMMENT_FIELDS + ",replies{" + COMMENT_FIELDS + "}"
CHILD_FIELDS = "children{id,media_type,media_url,thumbnail_url,permalink,timestamp}"
STORY_FIELDS = "id,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,caption"

# Insight metric names live in insights_metrics.py (version-sensitive).


# --------------------------------- plumbing --------------------------------- #

def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _summarize(result: Any) -> Any:
    """Compact a tool result for the audit io.output field (counts, not bodies)."""
    if isinstance(result, dict):
        summ: dict[str, Any] = {"ok": result.get("ok")}
        for k in ("count", "account", "media_id", "container_id", "comment_id", "id"):
            if k in result:
                summ[k] = result[k]
        return summ
    return {"type": type(result).__name__}


def _guard(tool: str, input_payload: dict[str, Any], impl: Any) -> dict[str, Any]:
    """Run a tool body with audit + fail-safe sanitized error shaping.

    Returns the impl() result on success, or a sanitized
    {ok: False, error, error_class} on any exception. No exception (and so no
    raw token in an exception message) escapes to the MCP seam.
    """
    start = time.perf_counter()
    safe_in = audit.sanitize_payload(input_payload)
    try:
        result = impl()
        audit.record(tool, execution_time_ms=_ms(start),
                     io={"input": safe_in, "output": _summarize(result)})
        return result
    except Exception as exc:  # noqa: BLE001 — boundary: classify + sanitize, never re-raise raw
        error_class = audit.classify_error(exc)
        message = audit.sanitize_error(str(exc))
        audit.record(tool, execution_time_ms=_ms(start),
                     io={"input": safe_in, "output": {"error": message}},
                     error_class=error_class)
        out = {"ok": False, "error": message, "error_class": error_class}
        extra = getattr(exc, "extra", None)
        if isinstance(extra, dict):
            out.update({k: v for k, v in extra.items() if k not in out})
        return out


def _account_summary(acct: auth.ResolvedAccount) -> dict[str, Any]:
    return {"label": acct.label, "ig_user_id": acct.ig_user_id, "base_host": acct.base_host}


# Per-tool ceilings for limit="all" (keeps one call inside Meta's ~200 calls/hour budget).
CEIL_MEDIA, CEIL_COMMENTS, CEIL_DISCOVERY, CEIL_TAGS, CEIL_CONVERSATIONS, CEIL_MESSAGES = (
    10000, 5000, 500, 1000, 200, 100)


def _paged(out: dict[str, Any], items: list[dict[str, Any]], cursor: str | None, key: str) -> dict[str, Any]:
    """Attach items + count, and next_cursor/truncated when more results exist."""
    out.update({"count": len(items), key: items})
    if cursor:
        out["next_cursor"] = cursor
        out["truncated"] = True
    return out


def _require_dm() -> None:
    """DM tools need instagram_manage_messages (Meta App Review). Fail loud, never stub."""
    enabled = os.environ.get("INSTAGRAM_MCP_DM_ENABLED", "").strip().lower() in {"1", "true", "yes"}
    if not enabled:
        raise graph_client.GraphAPIError(
            "Instagram DM tools require the instagram_manage_messages permission, granted only "
            "via Meta App Review (typically weeks). Once your app is approved AND the account token "
            "carries the scope, set INSTAGRAM_MCP_DM_ENABLED=1 to enable these tools.",
            error_class="needs_app_review",
        )


VIDEO_WAIT_S = int(os.environ.get("INSTAGRAM_MCP_VIDEO_WAIT_S", "300"))
PUBLISH_LATER_HINT = ("Instagram is still processing the video. Nothing was published yet. "
                      "Call publish_container(container_id) in a few minutes to finish without re-uploading.")


def _wait_container_ready(client: graph_client.GraphClient, container_id: str,
                          *, timeout_s: int | None = None, interval_s: int = 5) -> None:
    """Poll a media container until status_code=FINISHED (video/reel/story-video are async).

    On timeout raises a GraphAPIError(error_class=timeout) carrying container_id + a hint,
    so the caller can finish later with publish_container instead of re-uploading.
    """
    wait = VIDEO_WAIT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + wait
    while True:
        status = client.get(container_id, fields="status_code,status")
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise graph_client.GraphAPIError(
                f"media container {container_id} processing failed: {status.get('status')}",
                error_class="upstream_error")
        if time.monotonic() + interval_s > deadline:
            raise graph_client.GraphAPIError(
                f"media container {container_id} not ready after {wait}s", error_class="timeout",
                extra={"container_id": container_id, "status_code": code, "hint": PUBLISH_LATER_HINT})
        time.sleep(interval_s)


def _publish_container(client: graph_client.GraphClient, ig_user_id: str, container_id: str) -> dict[str, Any]:
    published = client.post(f"{ig_user_id}/media_publish", creation_id=container_id)
    return {"ok": True, "media_id": published.get("id"), "container_id": container_id}


# ============================ HEALTH + ACCOUNTS ============================ #

@mcp.tool()
def healthcheck() -> dict[str, Any]:
    """Verify the MCP is configured + ready: accounts present, token backend, Graph reachability.

    Does a live GET on the default account's profile if one is configured. Safe to
    call anytime; never mutates. Returns ok=false with a hint when no account is set.
    """
    def _impl() -> dict[str, Any]:
        accounts = auth.list_accounts()
        out: dict[str, Any] = {
            "ok": True,
            "accounts_configured": len(accounts),
            "secret_backend": auth.secret_backend(),
            "graph_version": graph_client.GRAPH_VERSION,
            "dm_enabled": os.environ.get("INSTAGRAM_MCP_DM_ENABLED", "").lower() in {"1", "true", "yes"},
            "default_account": next((a["label"] for a in accounts if a.get("default")), None),
            "live_check": None,
        }
        if not accounts:
            out["ok"] = False
            out["hint"] = ("No account configured. Set INSTAGRAM_MCP_ACCESS_TOKEN + "
                           "INSTAGRAM_MCP_IG_USER_ID, or call add_account(label, access_token, ig_user_id).")
            return out
        try:
            client, acct = auth.client_for(None)
            me = client.get(acct.ig_user_id, fields="id,username")
            out["live_check"] = {"ok": True, "username": me.get("username"), "account": acct.label}
            if graph_client.LAST_USAGE:
                usage = dict(graph_client.LAST_USAGE)
                if usage.get("max_percent", 0) >= 80:
                    usage["warning"] = ("Meta rate limit nearly reached; heavy calls (limit=\"all\") "
                                        "may be throttled for up to an hour.")
                out["api_usage"] = usage
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["live_check"] = {"ok": False, "error": audit.sanitize_error(str(exc)),
                                 "error_class": audit.classify_error(exc)}
        return out

    return _guard("healthcheck", {}, _impl)


@mcp.tool()
def list_accounts() -> dict[str, Any]:
    """List configured Instagram accounts (labels + ig_user_id + default flag). Never returns tokens."""
    return _guard("list_accounts", {}, lambda: {"ok": True, "accounts": auth.list_accounts(),
                                                 "count": len(auth.list_accounts())})


@mcp.tool()
def add_account(label: str, access_token: str, ig_user_id: str,
                *, app_secret: str | None = None, make_default: bool = False) -> dict[str, Any]:
    """Store an Instagram Business/Creator account for use by every tool.

    label        a short key you choose (e.g. "onde", "mycelium", "client-acme")
    access_token a long-lived Instagram Graph API token (stored in the OS keychain)
    ig_user_id   the Instagram Business Account id (numeric)
    app_secret   optional Meta app secret — enables appsecret_proof call hardening
    make_default set true to make this the account used when `account` is omitted

    The token is written to the macOS keychain (or a chmod-600 file fallback) and
    is NEVER echoed back or logged.
    """
    def _impl() -> dict[str, Any]:
        lbl = V.validate_account_label(label)
        uid = V.validate_ig_user_id(ig_user_id)
        if not access_token or not isinstance(access_token, str):
            raise V.ValidationError("access_token must be a non-empty string")
        info = auth.add_account(lbl, access_token, uid, app_secret=app_secret, make_default=make_default)
        return {"ok": True, **info}

    # Redact the token from the audit input — never persist it to the log.
    redacted = {"label": label, "access_token": "***", "token_len": len(access_token or ""),
                "ig_user_id": ig_user_id, "app_secret": "***" if app_secret else None,
                "make_default": make_default}
    return _guard("add_account", redacted, _impl)


@mcp.tool()
def set_default_account(label: str) -> dict[str, Any]:
    """Set which configured account is used when a tool's `account` arg is omitted."""
    return _guard("set_default_account", {"label": label},
                  lambda: {"ok": True, **auth.set_default(V.validate_account_label(label))})


@mcp.tool()
def remove_account(label: str) -> dict[str, Any]:
    """Remove a configured account + delete its stored token from the keychain."""
    return _guard("remove_account", {"label": label},
                  lambda: {"ok": True, **auth.remove_account(V.validate_account_label(label))})


@mcp.tool()
def account_info(account: str | None = None) -> dict[str, Any]:
    """Live profile snapshot for one account: username, name, followers, media count, etc."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        prof = client.get(acct.ig_user_id, fields=PROFILE_FIELDS)
        return {"ok": True, "account": acct.label, "profile": prof}
    return _guard("account_info", {"account": account}, _impl)


# ============================ PROFILE + MEDIA ============================ #

@mcp.tool()
def get_profile(account: str | None = None) -> dict[str, Any]:
    """Return the account's public profile fields (followers, follows, media count, bio, website)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        return {"ok": True, "account": acct.label, "profile": client.get(acct.ig_user_id, fields=PROFILE_FIELDS)}
    return _guard("get_profile", {"account": account}, _impl)


@mcp.tool()
def list_media(account: str | None = None, *, limit: int | str = 25, after: str | None = None,
               since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """List the account's posts/reels, newest first.

    limit: 1-10000 or "all" (default 25). since/until: dates like 2026-10-01 (inclusive)
    to keep only posts in that range. When more posts exist the result has
    next_cursor; pass it back as `after` to continue.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit, ceiling=CEIL_MEDIA, all_ceiling=CEIL_MEDIA)
        cur = V.validate_cursor(after)
        start_dt = V.validate_date(since, field="since")
        end_dt = V.validate_date(until, field="until", end_of_day=True)
        if start_dt and end_dt and start_dt > end_dt:
            raise V.ValidationError("since must be on or before until")

        def too_old(item: dict[str, Any]) -> bool:
            ts = item.get("timestamp")
            return bool(start_dt and ts and V.parse_graph_time(ts) < start_dt)

        items, cursor = client.paginate(f"{acct.ig_user_id}/media", fields=MEDIA_FIELDS,
                                        max_items=lim, after=cur, stop=too_old if start_dt else None)
        if end_dt:
            items = [m for m in items if not m.get("timestamp") or V.parse_graph_time(m["timestamp"]) <= end_dt]
        return _paged({"ok": True, "account": acct.label}, items, cursor, "media")
    return _guard("list_media", {"account": account, "limit": limit, "since": since, "until": until}, _impl)


@mcp.tool()
def get_media(media_id: str, account: str | None = None) -> dict[str, Any]:
    """Full fields for one post (caption, type, permalink, like/comment counts).

    Carousels also return `children` (each image/video with its own media_url).
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        return {"ok": True, "account": acct.label,
                "media": client.get(mid, fields=MEDIA_FIELDS + "," + CHILD_FIELDS)}
    return _guard("get_media", {"media_id": media_id, "account": account}, _impl)


# ================================ INSIGHTS ================================ #

def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


MAX_WINDOW_S = 30 * 86400          # Meta: at most 30 days between since and until
MAX_HISTORY_DAYS = 730             # Meta keeps ~2 years of account insights


def _resolve_range(since: str | None, until: str | None):
    """-> (since_dt, until_dt) or (None, None). Date-only `until` is inclusive (next midnight)."""
    from datetime import timedelta
    if not since and not until:
        return None, None
    now = _utcnow()
    end = V.validate_date(until, field="until")
    if end is not None and until and len(until.strip()) == 10:
        end = end + timedelta(days=1)
    end = min(end or now, now)
    start = V.validate_date(since, field="since") or (end - timedelta(days=30))
    if start >= end:
        raise V.ValidationError("since must be before until (and not in the future)")
    if start < now - timedelta(days=MAX_HISTORY_DAYS):
        raise V.ValidationError("Meta only keeps about 2 years of insights; use a later `since`")
    return start, end


def _windows(start, end):
    from datetime import timedelta
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(seconds=MAX_WINDOW_S), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def _account_insights_range(client, acct, ts: list[str], tv: list[str], start, end,
                            period: str = "day") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Fetch account insights over [start, end): chunked into 30-day windows and merged.

    Time-series metrics get their daily values stitched in order; total_value metrics are
    summed across windows (unique-user metrics like reach/accounts_engaged may then
    double-count across windows; flagged with `windows`). follower_count is only
    available for the last 30 days, so it is clamped to that range.
    """
    from datetime import timedelta
    merged: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    notes: list[str] = []

    def fetch(params, group, win):
        try:
            return client.get(f"{acct.ig_user_id}/insights", since=int(win[0].timestamp()),
                              until=int(win[1].timestamp()), **params).get("data", [])
        except graph_client.GraphAPIError as exc:
            errors.append({"metrics": group, "since": win[0].date().isoformat(),
                           "error": audit.sanitize_error(str(exc)), "error_class": exc.error_class})
            return []

    def add(rows, total: bool):
        for row in rows:
            name = row.get("name")
            slot = merged.setdefault(name, {"name": name, "period": row.get("period", period)})
            if total:
                val = ((row.get("total_value") or {}).get("value")) or 0
                slot.setdefault("total_value", {"value": 0})
                slot["total_value"]["value"] += val if isinstance(val, (int, float)) else 0
                slot["windows"] = slot.get("windows", 0) + 1
            else:
                slot.setdefault("values", []).extend(row.get("values") or [])

    ts_main = [m for m in ts if m != "follower_count"]
    for win in _windows(start, end):
        if ts_main:
            add(fetch({"metric": ",".join(ts_main), "period": period}, ts_main, win), total=False)
        if tv:
            add(fetch({"metric": ",".join(tv), "period": IM.TOTAL_VALUE_PERIOD,
                       "metric_type": "total_value"}, tv, win), total=True)
    if "follower_count" in ts:
        today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        f_start, f_end = max(start, today - timedelta(days=30)), min(end, today)
        if f_start < f_end:
            add(fetch({"metric": "follower_count", "period": "day"}, ["follower_count"], (f_start, f_end)),
                total=False)
        if start < today - timedelta(days=30) or f_start >= f_end:
            notes.append("follower_count is only available for the last 30 days (excluding today); "
                         "older days were skipped.")
    return list(merged.values()), errors, notes


@mcp.tool()
def get_account_insights(account: str | None = None, *, metrics: str | None = None,
                         period: str = "day", since: str | None = None,
                         until: str | None = None) -> dict[str, Any]:
    """Account-level analytics, optionally over a date range.

    metrics: comma-separated Graph metric names. Default: reach, follower_count (daily
    series) + views, profile_views, accounts_engaged, total_interactions (totals).
    since/until: dates like 2026-10-01 (until inclusive; up to ~2 years back). Longer
    ranges are split into 30-day requests (Meta's maximum) and merged: daily series are
    stitched, totals summed (see `windows`). follower_count only covers the last 30 days.
    Without since/until Meta returns its default recent window.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        names = IM.parse_metric_list(metrics, IM.DEFAULT_ACCOUNT_METRICS)
        per = V.validate_enum(period, {"day", "week", "days_28", "lifetime"}, field="period", default="day")
        ts, tv = IM.split_account_metrics(names)
        start, end = _resolve_range(since, until)
        notes: list[str] = []
        if start is not None:
            insights, errors, notes = _account_insights_range(client, acct, ts, tv, start, end, per)
        else:
            insights, errors = [], []
            groups = []
            if ts:
                groups.append(({"metric": ",".join(ts), "period": per}, ts))
            if tv:
                groups.append(({"metric": ",".join(tv), "period": IM.TOTAL_VALUE_PERIOD,
                                "metric_type": "total_value"}, tv))
            for params, group in groups:
                try:
                    insights.extend(client.get(f"{acct.ig_user_id}/insights", **params).get("data", []))
                except graph_client.GraphAPIError as exc:
                    errors.append({"metrics": group, "error": audit.sanitize_error(str(exc)),
                                   "error_class": exc.error_class})
        if not insights and errors:
            raise graph_client.GraphAPIError(errors[0]["error"], error_class=errors[0]["error_class"])
        out: dict[str, Any] = {"ok": True, "account": acct.label, "period": per, "insights": insights}
        if start is not None:
            out["since"], out["until"] = start.isoformat(), end.isoformat()
        if errors:
            out["errors"] = errors
        if notes:
            out["note"] = " ".join(notes)
        return out
    return _guard("get_account_insights", {"account": account, "metrics": metrics, "period": period,
                                           "since": since, "until": until}, _impl)


SORT_METRICS = frozenset({"reach", "views", "likes", "comments", "shares", "saved", "total_interactions"})
SUMMARY_TOTALS = ("views", "reach", "accounts_engaged", "total_interactions", "profile_views")
SUMMARY_PERIODS = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90}


def _metric_value(row: dict[str, Any]) -> Any:
    if row.get("total_value"):
        return row["total_value"].get("value")
    vals = row.get("values") or []
    return vals[0].get("value") if vals else None


def _batch_media_insights(client: graph_client.GraphClient, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch insights for many posts via Graph batch requests (50 per call).

    Sets post["metrics"] = {name: value} on success; returns per-post errors.
    """
    import json
    errors: list[dict[str, Any]] = []
    for i in range(0, len(posts), 50):
        chunk = posts[i:i + 50]
        batch = [{"method": "GET", "relative_url": f"{p['id']}/insights?metric="
                  + ",".join(IM.media_metrics_for(p.get("media_product_type")))} for p in chunk]
        replies = client.post("", batch=json.dumps(batch), include_headers="false").get("data") or []
        for post, reply in zip(chunk, replies, strict=False):
            try:
                body = json.loads((reply or {}).get("body") or "{}")
            except ValueError:
                body = {}
            if (reply or {}).get("code") != 200 or "error" in body:
                msg = ((body.get("error") or {}).get("message")) or "insights unavailable"
                errors.append({"id": post["id"], "error": audit.sanitize_error(msg)})
                continue
            post["metrics"] = {row["name"]: _metric_value(row) for row in body.get("data", [])}
    return errors


@mcp.tool()
def list_stories(account: str | None = None, *, include_insights: bool = False) -> dict[str, Any]:
    """Currently active stories (last 24 h) with media URLs and permalinks.

    include_insights=True adds reach, views, replies, shares, total_interactions per story.
    Story insights disappear after 24 h, so fetch them while the story is live.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        stories = client.get(f"{acct.ig_user_id}/stories", fields=STORY_FIELDS).get("data") or []
        for st in stories:
            st.setdefault("media_product_type", "STORY")
        out: dict[str, Any] = {"ok": True, "account": acct.label, "count": len(stories), "stories": stories}
        if include_insights and stories:
            errors = _batch_media_insights(client, stories)
            if errors:
                out["errors"] = errors
        return out
    return _guard("list_stories", {"account": account, "include_insights": include_insights}, _impl)


@mcp.tool()
def top_posts(account: str | None = None, *, since: str | None = None, until: str | None = None,
              metric: str = "reach", n: int = 10, media_type: str | None = None) -> dict[str, Any]:
    """Best-performing posts in a date range, ranked by an insight metric.

    since/until: dates like 2026-10-01 (default: last 30 days). metric: reach | views |
    likes | comments | shares | saved | total_interactions. media_type: optional filter
    (REELS, IMAGE, VIDEO, CAROUSEL_ALBUM, FEED). Per-post insights are fetched in Graph
    batch requests (50 posts per call) to save rate limit.
    """
    def _impl() -> dict[str, Any]:
        from datetime import timedelta
        client, acct = auth.client_for(account)
        if metric not in SORT_METRICS:
            raise V.ValidationError(f"metric must be one of {sorted(SORT_METRICS)}")
        top_n = V.validate_limit(n, field="n", default=10, ceiling=100)
        start, end = _resolve_range(since, until)
        if start is None:
            end = _utcnow()
            start = end - timedelta(days=30)
        kind = media_type.strip().upper() if media_type else None

        def too_old(item):
            ts = item.get("timestamp")
            return bool(ts and V.parse_graph_time(ts) < start)

        posts, _ = client.paginate(f"{acct.ig_user_id}/media", fields=MEDIA_FIELDS,
                                   max_items=CEIL_MEDIA, stop=too_old)
        posts = [p for p in posts if p.get("timestamp") and V.parse_graph_time(p["timestamp"]) < end]
        if kind:
            posts = [p for p in posts if kind in {(p.get("media_product_type") or "").upper(),
                                                   (p.get("media_type") or "").upper()}]
        errors = _batch_media_insights(client, posts)
        ranked = sorted((p for p in posts if "metrics" in p),
                        key=lambda p: p["metrics"].get(metric) or -1, reverse=True)[:top_n]
        result = [{"id": p["id"], "timestamp": p.get("timestamp"), "permalink": p.get("permalink"),
                   "media_type": p.get("media_type"), "media_product_type": p.get("media_product_type"),
                   "caption": (p.get("caption") or "")[:120], "metrics": p["metrics"]} for p in ranked]
        out: dict[str, Any] = {"ok": True, "account": acct.label, "metric": metric,
                               "since": start.isoformat(), "until": end.isoformat(),
                               "posts_considered": len(posts), "posts": result}
        if errors:
            out["errors"] = errors
        return out
    return _guard("top_posts", {"account": account, "since": since, "until": until, "metric": metric,
                                "n": n, "media_type": media_type}, _impl)


@mcp.tool()
def insights_summary(account: str | None = None, *, period: str = "last_30_days") -> dict[str, Any]:
    """Account totals for a recent period compared with the period before it.

    period: last_7_days | last_30_days | last_90_days. Returns views, reach,
    accounts_engaged, total_interactions, profile_views for both periods, % change,
    current follower count and new followers (last 30 days max).
    """
    def _impl() -> dict[str, Any]:
        from datetime import timedelta
        client, acct = auth.client_for(account)
        if period not in SUMMARY_PERIODS:
            raise V.ValidationError(f"period must be one of {sorted(SUMMARY_PERIODS)}")
        days = SUMMARY_PERIODS[period]
        today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        cur = (today - timedelta(days=days), today)
        prev = (today - timedelta(days=2 * days), cur[0])
        cur_rows, errs1, notes = _account_insights_range(client, acct, ["follower_count"],
                                                         list(SUMMARY_TOTALS), *cur)
        prev_rows, errs2, _ = _account_insights_range(client, acct, [], list(SUMMARY_TOTALS), *prev)

        def totals(rows):
            return {r["name"]: r["total_value"]["value"] for r in rows if "total_value" in r}

        current, previous = totals(cur_rows), totals(prev_rows)
        change = {k: (round((current[k] - previous[k]) / previous[k] * 100, 1) if previous.get(k) else None)
                  for k in current if k in previous}
        fc = next((r for r in cur_rows if r["name"] == "follower_count"), None)
        new_followers = sum(v.get("value") or 0 for v in (fc or {}).get("values", [])) if fc else None
        profile = client.get(acct.ig_user_id, fields="followers_count")
        out: dict[str, Any] = {"ok": True, "account": acct.label, "period": period,
                               "current_range": [cur[0].date().isoformat(), cur[1].date().isoformat()],
                               "previous_range": [prev[0].date().isoformat(), prev[1].date().isoformat()],
                               "current": current, "previous": previous, "change_pct": change,
                               "followers_now": profile.get("followers_count"),
                               "new_followers": new_followers}
        if days > 30:
            out["note"] = ("reach and accounts_engaged are summed over 30-day windows and may count the "
                           "same person more than once; new_followers covers the last 30 days only.")
        if errs1 or errs2:
            out["errors"] = errs1 + errs2
        return out
    return _guard("insights_summary", {"account": account, "period": period}, _impl)


@mcp.tool()
def get_media_insights(media_id: str, account: str | None = None, *, metrics: str | None = None) -> dict[str, Any]:
    """Per-post analytics. Picks the right default metrics for the post type.

    Feed posts/carousels: reach, likes, comments, shares, saved, total_interactions, views.
    Reels add ig_reels_avg_watch_time + ig_reels_video_view_total_time. Stories: reach,
    views, replies, shares, total_interactions. Pass `metrics` (comma-separated) to override.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        product = None
        if metrics:
            names = IM.parse_metric_list(metrics, ())
        else:
            info = client.get(mid, fields="media_product_type,media_type")
            product = info.get("media_product_type")
            names = list(IM.media_metrics_for(product))
        data = client.get(f"{mid}/insights", metric=",".join(names))
        out: dict[str, Any] = {"ok": True, "account": acct.label, "media_id": mid,
                               "insights": data.get("data", [])}
        if product:
            out["media_product_type"] = product
        return out
    return _guard("get_media_insights", {"media_id": media_id, "account": account, "metrics": metrics}, _impl)


AUDIENCE_HINT = ("Meta returns this when demographics are unavailable: the account needs "
                 ">=100 followers, and only one breakdown per request is allowed. Try a single "
                 "breakdown (age|gender|country|city) or, for engaged_audience_demographics, a "
                 "timeframe (this_week|this_month).")


@mcp.tool()
def get_audience_insights(account: str | None = None, *, metric: str | None = None,
                          period: str = "lifetime", metric_type: str = "total_value",
                          breakdown: str | None = "age,gender,country,city",
                          timeframe: str | None = None) -> dict[str, Any]:
    """Follower demographics by age, gender, country and city.

    Meta allows ONE breakdown per request, so each breakdown in `breakdown`
    (comma-separated, default all four) is fetched separately and merged into
    `breakdowns: {age: ..., gender: ...}`. metric: follower_demographics (default) or
    engaged_audience_demographics (pass timeframe=this_week|this_month for that one).
    Needs >=100 followers (Meta privacy floor).
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        m = metric or IM.DEFAULT_AUDIENCE_METRIC
        dims = IM.parse_metric_list(breakdown, IM.AUDIENCE_BREAKDOWNS)
        result: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        for dim in dims:
            params: dict[str, Any] = {"metric": m, "period": period, "metric_type": metric_type,
                                      "breakdown": dim, "timeframe": timeframe}
            try:
                result[dim] = client.get(f"{acct.ig_user_id}/insights", **params).get("data", [])
            except graph_client.GraphAPIError as exc:
                errors.append({"breakdown": dim, "error": audit.sanitize_error(str(exc)),
                               "error_class": exc.error_class})
        if not result:
            return {"ok": False, "account": acct.label, "metric": m,
                    "error": errors[0]["error"] if errors else "no breakdowns requested",
                    "error_class": errors[0]["error_class"] if errors else "validation",
                    "errors": errors, "hint": AUDIENCE_HINT}
        out: dict[str, Any] = {"ok": True, "account": acct.label, "metric": m, "breakdowns": result}
        if errors:
            out["errors"] = errors
            out["hint"] = AUDIENCE_HINT
        return out
    return _guard("get_audience_insights",
                  {"account": account, "metric": metric, "period": period, "breakdown": breakdown}, _impl)


# =============================== PUBLISHING =============================== #

@mcp.tool()
def publishing_limit(account: str | None = None) -> dict[str, Any]:
    """Remaining posts in the rolling 24h publishing quota (Instagram caps API posts/day)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        data = client.get(f"{acct.ig_user_id}/content_publishing_limit",
                           fields="config,quota_usage")
        return {"ok": True, "account": acct.label, "limit": data.get("data", [])}
    return _guard("publishing_limit", {"account": account}, _impl)


@mcp.tool()
def publish_container(container_id: str, account: str | None = None) -> dict[str, Any]:
    """Finish publishing a media container created earlier (e.g. a video that was still
    processing when publish_video/publish_reel/publish_story timed out).

    Publishes if processing is FINISHED; otherwise returns ok=false with its status_code.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(container_id, field="container_id")
        status = client.get(cid, fields="status_code,status")
        code = status.get("status_code")
        if code == "FINISHED":
            return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
        if code == "ERROR":
            raise graph_client.GraphAPIError(f"media container {cid} processing failed: {status.get('status')}",
                                             error_class="upstream_error")
        return {"ok": False, "account": acct.label, "container_id": cid, "status_code": code,
                "error_class": "not_ready", "hint": "Still processing; try again in a minute."}
    return _guard("publish_container", {"container_id": container_id, "account": account}, _impl)


@mcp.tool()
def publish_image(image_url: str, caption: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Publish a single image to the feed.

    image_url must be a PUBLIC https URL (Instagram fetches the bytes itself). caption
    <= 2200 chars. Two-step Graph flow (create container -> publish) handled internally.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(image_url, field="image_url")
        cap = V.validate_caption(caption)
        container = client.post(f"{acct.ig_user_id}/media", image_url=url, caption=cap)
        cid = container.get("id")
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_image", {"image_url": image_url, "caption": V.truncate(caption or ""),
                                    "account": account}, _impl)


@mcp.tool()
def publish_video(video_url: str, caption: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Publish a video to the feed. video_url must be a PUBLIC https URL.

    Video containers process asynchronously; this polls status up to 90s before publishing.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(video_url, field="video_url")
        cap = V.validate_caption(caption)
        container = client.post(f"{acct.ig_user_id}/media", media_type="VIDEO", video_url=url, caption=cap)
        cid = container.get("id")
        _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_video", {"video_url": video_url, "caption": V.truncate(caption or ""),
                                    "account": account}, _impl)


@mcp.tool()
def publish_reel(video_url: str, caption: str | None = None, account: str | None = None,
                 *, share_to_feed: bool = True, cover_url: str | None = None) -> dict[str, Any]:
    """Publish a Reel. video_url must be a PUBLIC https URL.

    share_to_feed also surfaces the reel in the main grid. cover_url (optional) sets the
    thumbnail. Reel containers process asynchronously (polled up to 90s).
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        url = V.validate_public_https_url(video_url, field="video_url")
        cap = V.validate_caption(caption)
        cover = V.validate_public_https_url(cover_url, field="cover_url") if cover_url else None
        container = client.post(f"{acct.ig_user_id}/media", media_type="REELS", video_url=url,
                                caption=cap, share_to_feed=share_to_feed, cover_url=cover)
        cid = container.get("id")
        _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_reel", {"video_url": video_url, "caption": V.truncate(caption or ""),
                                   "share_to_feed": share_to_feed, "account": account}, _impl)


@mcp.tool()
def publish_carousel(image_urls: list[str], caption: str | None = None,
                     account: str | None = None) -> dict[str, Any]:
    """Publish a multi-image carousel (2-10 images). Each image_url must be a PUBLIC https URL.

    Creates one child container per image (is_carousel_item) then a CAROUSEL parent, then publishes.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        if not isinstance(image_urls, list) or not (2 <= len(image_urls) <= 10):
            raise V.ValidationError("image_urls must be a list of 2-10 public https URLs")
        urls = [V.validate_public_https_url(u, field="image_url") for u in image_urls]
        cap = V.validate_caption(caption)
        child_ids: list[str] = []
        for u in urls:
            child = client.post(f"{acct.ig_user_id}/media", image_url=u, is_carousel_item=True)
            child_ids.append(child.get("id"))
        parent = client.post(f"{acct.ig_user_id}/media", media_type="CAROUSEL",
                             children=",".join(child_ids), caption=cap)
        cid = parent.get("id")
        return {"account": acct.label, "child_count": len(child_ids),
                **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_carousel", {"image_count": len(image_urls or []),
                                       "caption": V.truncate(caption or ""), "account": account}, _impl)


@mcp.tool()
def publish_story(image_url: str | None = None, video_url: str | None = None,
                  account: str | None = None) -> dict[str, Any]:
    """Publish a Story (image OR video). Exactly one of image_url / video_url, PUBLIC https."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        if bool(image_url) == bool(video_url):
            raise V.ValidationError("pass exactly one of image_url or video_url")
        if image_url:
            url = V.validate_public_https_url(image_url, field="image_url")
            container = client.post(f"{acct.ig_user_id}/media", media_type="STORIES", image_url=url)
            cid = container.get("id")
        else:
            url = V.validate_public_https_url(video_url, field="video_url")
            container = client.post(f"{acct.ig_user_id}/media", media_type="STORIES", video_url=url)
            cid = container.get("id")
            _wait_container_ready(client, cid)
        return {"account": acct.label, **_publish_container(client, acct.ig_user_id, cid)}
    return _guard("publish_story", {"image_url": image_url, "video_url": video_url, "account": account}, _impl)


# ================================ COMMENTS ================================ #

@mcp.tool()
def get_comments(media_id: str, account: str | None = None, *, limit: int | str = 25,
                 after: str | None = None, include_replies: bool = False) -> dict[str, Any]:
    """List top-level comments on a post (text, username, timestamp, like_count, hidden).

    limit: 1-5000 or "all". include_replies=True also returns each comment's replies
    (for a very long thread use get_comment_replies). Note: Meta's comments_count on a
    post also counts comments the API does not return (e.g. hidden/restricted).
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        mid = V.validate_graph_id(media_id, field="media_id")
        lim = V.validate_limit(limit, ceiling=CEIL_COMMENTS, all_ceiling=CEIL_COMMENTS)
        fields = COMMENT_WITH_REPLIES_FIELDS if include_replies else COMMENT_FIELDS
        items, cursor = client.paginate(f"{mid}/comments", fields=fields, max_items=lim,
                                        after=V.validate_cursor(after))
        out = _paged({"ok": True, "account": acct.label, "media_id": mid}, items, cursor, "comments")
        if include_replies:
            out["reply_count"] = sum(len((c.get("replies") or {}).get("data") or []) for c in items)
        return out
    return _guard("get_comments", {"media_id": media_id, "account": account, "limit": limit}, _impl)


@mcp.tool()
def get_comment_replies(comment_id: str, account: str | None = None, *, limit: int | str = 50,
                        after: str | None = None) -> dict[str, Any]:
    """List replies under one comment. limit 1-5000 or "all"; returns next_cursor if more."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        lim = V.validate_limit(limit, default=50, ceiling=CEIL_COMMENTS, all_ceiling=CEIL_COMMENTS)
        items, cursor = client.paginate(f"{cid}/replies", fields=COMMENT_FIELDS, max_items=lim,
                                        after=V.validate_cursor(after))
        return _paged({"ok": True, "account": acct.label, "comment_id": cid}, items, cursor, "replies")
    return _guard("get_comment_replies", {"comment_id": comment_id, "account": account, "limit": limit}, _impl)


@mcp.tool()
def reply_to_comment(comment_id: str, message: str, account: str | None = None) -> dict[str, Any]:
    """Reply to a comment. message <= 2200 chars. Returns the new comment id."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        msg = V.validate_caption(message, field="message")
        if not msg:
            raise V.ValidationError("message must be non-empty")
        res = client.post(f"{cid}/replies", message=msg)
        return {"ok": True, "account": acct.label, "comment_id": res.get("id")}
    return _guard("reply_to_comment", {"comment_id": comment_id, "message": V.truncate(message),
                                       "account": account}, _impl)


@mcp.tool()
def hide_comment(comment_id: str, hide: bool = True, account: str | None = None) -> dict[str, Any]:
    """Hide (or unhide) a comment from public view. hide=false unhides."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        client.post(cid, hide=bool(hide))
        return {"ok": True, "account": acct.label, "comment_id": cid, "hidden": bool(hide)}
    return _guard("hide_comment", {"comment_id": comment_id, "hide": hide, "account": account}, _impl)


@mcp.tool()
def delete_comment(comment_id: str, account: str | None = None) -> dict[str, Any]:
    """Permanently delete a comment you own (or a comment on your media). Irreversible."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        cid = V.validate_graph_id(comment_id, field="comment_id")
        client.delete(cid)
        return {"ok": True, "account": acct.label, "comment_id": cid, "deleted": True}
    return _guard("delete_comment", {"comment_id": comment_id, "account": account}, _impl)


# ================================ DISCOVERY ================================ #

@mcp.tool()
def search_hashtag(hashtag: str, account: str | None = None) -> dict[str, Any]:
    """Resolve a hashtag name to its Graph id (needed before get_hashtag_media)."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        tag = V.validate_hashtag(hashtag)
        res = client.get("ig_hashtag_search", user_id=acct.ig_user_id, q=tag)
        return {"ok": True, "account": acct.label, "hashtag": tag, "results": res.get("data", [])}
    return _guard("search_hashtag", {"hashtag": hashtag, "account": account}, _impl)


@mcp.tool()
def get_hashtag_media(hashtag_id: str, account: str | None = None, *,
                      edge: str = "top_media", limit: int | str = 25,
                      after: str | None = None) -> dict[str, Any]:
    """Recent or top media for a hashtag id. edge=top_media|recent_media. limit 1-1000 or "all".

    Get the hashtag_id from search_hashtag first. Subject to Meta's 30-unique-hashtags
    per-7-days query limit per account.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        hid = V.validate_graph_id(hashtag_id, field="hashtag_id")
        e = V.validate_enum(edge, {"top_media", "recent_media"}, field="edge", default="top_media")
        lim = V.validate_limit(limit, ceiling=CEIL_TAGS, all_ceiling=CEIL_TAGS)
        items, cursor = client.paginate(f"{hid}/{e}", user_id=acct.ig_user_id,
                                        fields="id,caption,media_type,permalink,like_count,comments_count,timestamp",
                                        max_items=lim, after=V.validate_cursor(after), page_size=50)
        return _paged({"ok": True, "account": acct.label, "hashtag_id": hid, "edge": e},
                      items, cursor, "media")
    return _guard("get_hashtag_media", {"hashtag_id": hashtag_id, "edge": edge, "limit": limit,
                                        "account": account}, _impl)


@mcp.tool()
def get_mentions(account: str | None = None, *, limit: int | str = 25,
                 after: str | None = None) -> dict[str, Any]:
    """List media where the account is tagged/@-mentioned. limit 1-1000 or "all"."""
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit, ceiling=CEIL_TAGS, all_ceiling=CEIL_TAGS)
        items, cursor = client.paginate(f"{acct.ig_user_id}/tags", fields=MEDIA_FIELDS, max_items=lim,
                                        after=V.validate_cursor(after))
        return _paged({"ok": True, "account": acct.label}, items, cursor, "media")
    return _guard("get_mentions", {"account": account, "limit": limit}, _impl)


BD_PROFILE_FIELDS = ("username,name,biography,followers_count,follows_count,media_count,"
                     "profile_picture_url,website")
BD_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,like_count,comments_count,timestamp"


@mcp.tool()
def business_discovery(username: str, account: str | None = None, *, with_media: bool = False,
                       media_limit: int | str = 12) -> dict[str, Any]:
    """Public profile + (optionally) recent posts of ANY business/creator account by username.

    Read-only competitor/prospect research. Only works for Professional accounts.
    with_media pulls up to media_limit recent posts (1-500 or "all"), paging as needed.
    """
    def _impl() -> dict[str, Any]:
        client, acct = auth.client_for(account)
        uname = V.validate_username(username)
        lim = V.validate_limit(media_limit, ceiling=CEIL_DISCOVERY, all_ceiling=CEIL_DISCOVERY,
                               default=12, field="media_limit") if with_media else 0
        profile: dict[str, Any] | None = None
        posts: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            fields = f"business_discovery.username({uname}){{{BD_PROFILE_FIELDS}"
            if with_media:
                n = min(100, lim - len(posts))
                fields += f",media.limit({n})" + (f".after({cursor})" if cursor else "") + f"{{{BD_MEDIA_FIELDS}}}"
            fields += "}"
            bd = client.get(acct.ig_user_id, fields=fields).get("business_discovery", {})
            if profile is None:
                profile = {k: v for k, v in bd.items() if k != "media"}
            if not with_media:
                break
            media = bd.get("media") or {}
            batch = media.get("data") or []
            posts.extend(batch)
            cursor = ((media.get("paging") or {}).get("cursors") or {}).get("after")
            if not batch or not cursor or len(posts) >= lim:
                break
            cursor = V.validate_cursor(cursor, field="cursor")
        result: dict[str, Any] = dict(profile or {})
        if with_media:
            result["media"] = {"data": posts[:lim]}
        out: dict[str, Any] = {"ok": True, "account": acct.label, "target": uname,
                               "business_discovery": result}
        if with_media and cursor and len(posts) >= lim:
            out["truncated"] = True
        return out
    return _guard("business_discovery", {"username": username, "with_media": with_media,
                                         "account": account}, _impl)


# ============================= DIRECT MESSAGES ============================= #
# Gated behind Meta App Review (instagram_manage_messages). Each tool fails loud
# with the app-review hint unless INSTAGRAM_MCP_DM_ENABLED=1 AND the token carries
# the scope. Scaffolded (not stubbed) so enabling is a one-flag flip post-approval.

_PAGE_CACHE: dict = {}


def _page_dm_client(client, acct):
    """IG messaging (Facebook-Login path) works reliably only with the PAGE token,
    on the Page node. Resolve the Page linked to this IG account and return
    (page_client, page_id). Slow Meta responses -> generous timeout."""
    from instagram_mcp import graph_client as _gc
    if acct.label in _PAGE_CACHE:
        return _PAGE_CACHE[acct.label]
    slow = _gc.GraphClient(acct.access_token, base_host=acct.base_host,
                           app_secret=acct.app_secret, timeout=120.0)
    res = slow.get("me/accounts", fields="id,access_token,instagram_business_account", limit=100)
    for pg in res.get("data", []):
        if (pg.get("instagram_business_account") or {}).get("id") == acct.ig_user_id:
            out = (_gc.GraphClient(pg["access_token"], base_host=acct.base_host,
                                   app_secret=acct.app_secret, timeout=120.0), pg["id"])
            _PAGE_CACHE[acct.label] = out
            return out
    raise V.ValidationError("no Facebook Page linked to this Instagram account was found in me/accounts")


@mcp.tool()
def list_conversations(account: str | None = None, *, limit: int | str = 5,
                       after: str | None = None) -> dict[str, Any]:
    """List Instagram DM conversations (newest first) with participant usernames.
    REQUIRES Meta App Review (instagram_manage_messages). limit 1-200 or "all" (default 5).
    Fetched one conversation per request: Meta rejects larger pages with
    'Please reduce the amount of data you're asking for'."""
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        lim = V.validate_limit(limit, ceiling=CEIL_CONVERSATIONS, all_ceiling=CEIL_CONVERSATIONS)
        pclient, page_id = _page_dm_client(client, acct)
        from instagram_mcp import graph_client as _gc
        items: list = []
        page = pclient.get(f"{page_id}/conversations", platform="instagram",
                           fields="id,updated_time", limit=1, after=V.validate_cursor(after))
        note = None
        nxt: str | None = None
        while True:
            items.extend(page.get("data") or [])
            nxt = (page.get("paging") or {}).get("cursors", {}).get("after")
            if not nxt or not (page.get("paging") or {}).get("next"):
                nxt = None
                break
            if len(items) >= lim:
                break
            try:  # later pages can hang in dev mode (hidden non-role conversations)
                short = _gc.GraphClient(pclient._token, base_host=acct.base_host,
                                        app_secret=acct.app_secret, timeout=20.0)
                page = short.get(f"{page_id}/conversations", platform="instagram",
                                 fields="id,updated_time", limit=1, after=nxt)
            except Exception:
                nxt = None
                note = "stopped early: Meta did not return more conversations (normal in development mode)"
                break
        items = items[:lim]
        for c in items:
            try:
                parts = pclient.get(c["id"], fields="participants").get("participants", {}).get("data", [])
                c["participants"] = [{"username": p.get("username"), "id": p.get("id")} for p in parts]
            except Exception as exc:  # keep listing even if one lookup fails
                c["participants_error"] = audit.sanitize_error(str(exc))
        out = _paged({"ok": True, "account": acct.label}, items, nxt, "conversations")
        if note:
            out["note"] = note
        return out
    return _guard("list_conversations", {"account": account, "limit": limit}, _impl)


@mcp.tool()
def get_messages(conversation_id: str, account: str | None = None, *, limit: int = 10) -> dict[str, Any]:
    """Read messages in a DM conversation, newest first. limit 1-25 (default 10).
    REQUIRES Meta App Review (instagram_manage_messages)."""
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        cid = V.validate_conversation_id(conversation_id)
        lim = V.validate_limit(limit, ceiling=CEIL_MESSAGES, all_ceiling=CEIL_MESSAGES)
        pclient, _ = _page_dm_client(client, acct)
        res = pclient.get(cid, fields="messages.limit(" + str(lim) + "){id,created_time,from,to,message}")
        return {"ok": True, "account": acct.label, "conversation_id": cid,
                "messages": ((res.get("messages") or {}).get("data") or [])[:lim]}
    return _guard("get_messages", {"conversation_id": conversation_id, "account": account, "limit": limit}, _impl)


@mcp.tool()
def send_message(recipient_id: str, text: str, account: str | None = None) -> dict[str, Any]:
    """Send a DM. REQUIRES Meta App Review + the 24-hour standard-messaging window.

    recipient_id is the IGSID (Instagram-scoped user id) of the recipient — the
    participant "id" from list_conversations / "from.id" from get_messages.
    Outside the 24h customer-service window a message tag is required (not handled here).
    """
    def _impl() -> dict[str, Any]:
        _require_dm()
        client, acct = auth.client_for(account)
        rid = V.validate_graph_id(recipient_id, field="recipient_id")
        msg = V.validate_caption(text, field="text")
        if not msg:
            raise V.ValidationError("text must be non-empty")
        pclient, page_id = _page_dm_client(client, acct)
        res = pclient.post(f"{page_id}/messages",
                           recipient='{"id":"' + rid + '"}', message='{"text":' + _json_str(msg) + '}')
        return {"ok": True, "account": acct.label, "recipient_id": rid, "result": res}
    return _guard("send_message", {"recipient_id": recipient_id, "text": V.truncate(text), "account": account}, _impl)


def _json_str(s: str) -> str:
    """Minimal JSON string encoder for the messages payload."""
    import json
    return json.dumps(s, ensure_ascii=False)


def run() -> None:
    """Entrypoint for the ``instagram-mcp`` console script and ``python -m`` execution."""
    mcp.run()


if __name__ == "__main__":
    run()
