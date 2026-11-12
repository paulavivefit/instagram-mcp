"""Instagram insight metric tables (verified against the live Graph API, 2026-11).

Meta changes metric names between Graph versions (e.g. `impressions` was retired in
favour of `views`). Keep every version-sensitive name here so a change is one edit.
"""

from __future__ import annotations

# Account-level metrics that come back as a time series with `period`.
ACCOUNT_TIME_SERIES = frozenset({"reach", "follower_count", "online_followers"})

# Everything else at account level must be requested with metric_type=total_value,
# and Meta only accepts period=day for those (week/days_28 are rejected).
TOTAL_VALUE_PERIOD = "day"

DEFAULT_ACCOUNT_METRICS = (
    "reach",
    "follower_count",
    "views",
    "profile_views",
    "accounts_engaged",
    "total_interactions",
)

DEFAULT_AUDIENCE_METRIC = "follower_demographics"
AUDIENCE_BREAKDOWNS = ("age", "gender", "country", "city")

_FEED = ("reach", "likes", "comments", "shares", "saved", "total_interactions", "views")
_REELS = _FEED + ("ig_reels_avg_watch_time", "ig_reels_video_view_total_time")
_STORY = ("reach", "views", "replies", "shares", "total_interactions")

MEDIA_METRICS_BY_PRODUCT = {"FEED": _FEED, "REELS": _REELS, "STORY": _STORY}


def split_account_metrics(metrics: list[str]) -> tuple[list[str], list[str]]:
    """Split metric names into (time_series, total_value), preserving order."""
    ts = [m for m in metrics if m in ACCOUNT_TIME_SERIES]
    tv = [m for m in metrics if m not in ACCOUNT_TIME_SERIES]
    return ts, tv


def media_metrics_for(media_product_type: str | None) -> tuple[str, ...]:
    """Default per-media metrics for a media_product_type (FEED / REELS / STORY / AD)."""
    return MEDIA_METRICS_BY_PRODUCT.get((media_product_type or "FEED").upper(), _FEED)


def parse_metric_list(value: str | list[str] | None, default: tuple[str, ...]) -> list[str]:
    """Accept 'a,b' or ['a','b'] (or None -> default); strip blanks and duplicates."""
    if value is None or value == "" or value == []:
        items = list(default)
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    out: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out
