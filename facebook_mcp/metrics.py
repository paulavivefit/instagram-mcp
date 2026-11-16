"""Facebook Page / post insight metric names verified live (Graph v27, 2026-11-16).

Meta retired the impression-based metrics in 2025 (page_impressions*, page_fans*,
page_engaged_users, post_impressions*, post_engaged_users). They now return
"(#100) The value must be a valid insights metric".
"""

from __future__ import annotations

PAGE_METRICS: dict[str, str] = {
    "page_media_view": "Times your content was shown on screen (views)",
    "page_total_media_view_unique": "People who saw your content (reach)",
    "page_post_engagements": "Reactions, comments, shares and clicks on your posts",
    "page_actions_post_reactions_total": "Reactions by type",
    "page_follows": "Total followers (snapshot)",
    "page_daily_follows_unique": "New followers",
    "page_daily_unfollows_unique": "Unfollows",
    "page_views_total": "Page profile visits",
    "page_video_views": "Video plays (3s+)",
    "page_video_complete_views_30s": "Video plays of 30s+ (or complete)",
    "page_video_view_time": "Total video watch time in ms",
}
# Snapshot breakdowns (only period=day; a dict of city/country -> followers).
PAGE_BREAKDOWN_METRICS: dict[str, str] = {
    "page_follows_city": "Followers by city (top cities)",
    "page_follows_country": "Followers by country code",
}
DEFAULT_PAGE_METRICS = (
    "page_media_view",
    "page_total_media_view_unique",
    "page_post_engagements",
    "page_daily_follows_unique",
    "page_daily_unfollows_unique",
    "page_views_total",
    "page_video_views",
)
# Summable over days (snapshot metrics like page_follows are not).
SUMMABLE = frozenset(
    {
        "page_media_view",
        "page_post_engagements",
        "page_daily_follows_unique",
        "page_daily_unfollows_unique",
        "page_views_total",
        "page_video_views",
        "page_video_complete_views_30s",
        "page_video_view_time",
    }
)

POST_METRICS: dict[str, str] = {
    "post_media_view": "Times the post was shown (views)",
    "post_total_media_view_unique": "People who saw the post (reach)",
    "post_clicks": "Clicks on the post",
    "post_clicks_by_type": "Clicks by type (photo view, link clicks, other)",
    "post_reactions_by_type_total": "Reactions by type",
    "post_activity_by_action_type": "Likes, comments and shares",
}
POST_VIDEO_METRICS: dict[str, str] = {
    "post_video_views": "Video plays (3s+)",
    "post_video_views_organic": "Organic video plays",
    "post_video_avg_time_watched": "Average watch time in ms",
    "post_video_complete_views_30s": "Plays of 30s+ (or complete)",
    "post_video_view_time": "Total watch time in ms",
    "post_video_retention_graph": "Share of viewers still watching, by second",
}
DEFAULT_POST_METRICS = tuple(POST_METRICS)

RETIRED_HINT = (
    "Meta retired impression-based metrics in 2025. Use page_media_view (views) and "
    "page_total_media_view_unique (reach) for Pages, post_media_view / post_total_media_view_unique for posts."
)
