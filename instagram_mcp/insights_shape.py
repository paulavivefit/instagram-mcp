"""Turn Meta's insight payloads into small, honest, comparable values.

Rules verified against the live Graph API (2026-11):
- Daily values carry `end_time` = Pacific-time midnight that ENDS the day, so the value
  belongs to the Pacific calendar day before it (08:00 UTC in winter, 07:00 UTC in summer).
- For older periods Meta returns 0 (or even negative numbers) for metrics it did not
  collect yet, e.g. account `views` before mid-2025. A 0 next to real activity is not data.
- Old reels and pre-2023 feed posts report a `reach` far below `views`/`likes`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

UNAVAILABLE_REASON = (
    "Meta returned no usable value for this period (0 or negative while the account "
    "had activity); the metric was probably not collected yet."
)
REACH_UNDERREPORTED = (
    "reach looks under-reported by Meta for this post (much lower than views/likes); "
    "common for reels before 2025 and older feed posts. Don't compare it with recent posts."
)
VIEWS_UNAVAILABLE = "Meta has no views for this post (the metric did not exist when it was published)."


def pacific_date(end_time: str) -> str:
    """Calendar day (Pacific) that a Meta daily value covers: end_time minus one second."""
    end = datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%S%z")
    return (end - timedelta(seconds=1)).astimezone(PACIFIC).date().isoformat()


def pacific_midnight(day: str | Any) -> datetime:
    """UTC instant of 00:00 Pacific on the given date ("YYYY-MM-DD" or date)."""
    d = datetime.strptime(day, "%Y-%m-%d").date() if isinstance(day, str) else day
    return datetime.combine(d, time.min, tzinfo=PACIFIC).astimezone(timezone.utc)


def pacific_today(now: datetime) -> Any:
    return now.astimezone(PACIFIC).date()


def flatten_series(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for v in values or []:
        end = v.get("end_time")
        out.append({"date": pacific_date(end) if end else None, "value": v.get("value")})
    return out


# Metrics Meta did not collect for older periods (live: 0 next to real reach before mid-2025,
# total_interactions even negative). A 0 for any other metric is treated as a real zero.
COLLECTION_GAP_METRICS = frozenset({"views", "accounts_engaged", "total_interactions"})


def clean_window_totals(window: dict[str, Any], activity: float) -> tuple[dict[str, Any], dict[str, str]]:
    """Null out totals that are not real data for one window.

    A value is unavailable when it is missing or negative, or - for metrics with known
    collection gaps - 0 while `activity` (reach or another non-zero total) shows activity.
    """
    others = [v for v in window.values() if isinstance(v, (int, float)) and v > 0]
    active = activity > 0 or bool(others)
    clean: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    for name, value in window.items():
        gap_zero = value == 0 and active and name in COLLECTION_GAP_METRICS
        bad = not isinstance(value, (int, float)) or value < 0 or gap_zero
        if bad:
            clean[name] = None
            unavailable[name] = UNAVAILABLE_REASON
        else:
            clean[name] = value
    return clean, unavailable


def row_value(row: dict[str, Any]) -> Any:
    if row.get("total_value") and "value" in row["total_value"]:
        return row["total_value"]["value"]
    vals = row.get("values") or []
    return vals[0].get("value") if vals else None


def media_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {row["name"]: row_value(row) for row in rows if isinstance(row, dict) and row.get("name")}


def media_quality(product_type: str | None, metrics: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Null impossible values and warn about Meta's under-reported reach on old posts."""
    m = dict(metrics)
    warnings: list[str] = []
    likes = m.get("likes") or 0
    views = m.get("views")
    reach = m.get("reach")
    if views == 0 and likes > 0:
        m["views"] = None
        warnings.append(VIEWS_UNAVAILABLE)
        views = None
    if isinstance(reach, (int, float)):
        low_vs_views = isinstance(views, (int, float)) and views > 0 and reach < views / 20
        low_vs_likes = likes > 0 and reach < likes
        if low_vs_views or low_vs_likes:
            warnings.append(REACH_UNDERREPORTED)
    return m, warnings


def audience_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{key, value, percent}] sorted by value (largest first) from a demographics row."""
    results: list[dict[str, Any]] = []
    for row in rows or []:
        for bd in (row.get("total_value") or {}).get("breakdowns") or []:
            for r in bd.get("results") or []:
                key = ", ".join(str(x) for x in (r.get("dimension_values") or []))
                results.append({"key": key, "value": r.get("value") or 0})
    total = sum(r["value"] for r in results) or 0
    for r in results:
        r["percent"] = round(r["value"] / total * 100, 1) if total else None
    return sorted(results, key=lambda r: r["value"], reverse=True)
