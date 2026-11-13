"""INPUT rail for instagram-mcp.

Every tool validates its inputs through this module BEFORE any Graph API call,
per the MCP Build Runbook §"INPUT rail" + llm-rails-taxonomy.md §INPUT stage.

Validation is deliberately strict + allocation-free: it raises ValidationError
(mapped to error_class="validation" by audit.classify_error) rather than letting
a malformed id reach graph.facebook.com and bounce back as an opaque OAuthException.
"""

from __future__ import annotations

import re
from typing import Any

# Instagram caption hard limit (Meta-documented).
CAPTION_MAX = 2200
# Instagram allows up to 30 hashtags per post; we cap-validate the count elsewhere.
HASHTAG_MAX_LEN = 100

_GRAPH_ID_RE = re.compile(r"^[0-9A-Za-z_]{1,64}$")
_NUMERIC_ID_RE = re.compile(r"^[0-9]{1,32}$")
_HASHTAG_RE = re.compile(r"^[0-9A-Za-z_]{1,100}$")
_ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,62}$")


class ValidationError(ValueError):
    """Raised when a tool input fails the INPUT rail. Maps to error_class=validation."""


def truncate(text: Any, limit: int = 200) -> str:
    """Shorten a string for the audit io field (never the real payload)."""
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def validate_ig_user_id(value: str, *, field: str = "ig_user_id") -> str:
    """An Instagram Business/Creator account id is a numeric Graph node id."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _NUMERIC_ID_RE.match(v):
        raise ValidationError(f"{field} must be a numeric Instagram account id (got {truncate(v, 40)!r})")
    return v


def validate_graph_id(value: str, *, field: str = "id") -> str:
    """A generic Graph object id (media / comment / container / page)."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _GRAPH_ID_RE.match(v):
        raise ValidationError(
            f"{field} must be a Graph object id (alphanumeric/underscore, <=64; got {truncate(v, 40)!r})"
        )
    return v


_CONVERSATION_ID_RE = re.compile(r"^[0-9A-Za-z_\-=]{1,256}$")


def validate_conversation_id(value: str | None, *, field: str = "conversation_id") -> str:
    """An IG conversation id: long base64-style string (letters, digits, - _ =).

    Unlike media ids these exceed 64 chars; path/query characters (/ ? & space) are
    rejected so the id can never alter the request path.
    """
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _CONVERSATION_ID_RE.match(v):
        raise ValidationError(f"{field} must be the id returned by list_conversations")
    return v


def validate_caption(value: str | None, *, field: str = "caption") -> str | None:
    """Instagram caption: <= 2200 chars. None passes through (caption is optional)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if len(value) > CAPTION_MAX:
        raise ValidationError(f"{field} exceeds Instagram's {CAPTION_MAX}-char limit (got {len(value)})")
    tags = len(_HASHTAG_IN_TEXT.findall(value))
    if tags > MAX_CAPTION_HASHTAGS:
        raise ValidationError(f"{field} has {tags} hashtags; Instagram allows at most {MAX_CAPTION_HASHTAGS}")
    mentions = len(_MENTION_IN_TEXT.findall(value))
    if mentions > MAX_CAPTION_MENTIONS:
        raise ValidationError(f"{field} has {mentions} @mentions; Instagram allows at most {MAX_CAPTION_MENTIONS}")
    return value


MAX_CAPTION_HASHTAGS = 30
MAX_CAPTION_MENTIONS = 20
MAX_USER_TAGS = 20
MAX_COLLABORATORS = 3
ALT_TEXT_MAX = 1000
_HASHTAG_IN_TEXT = re.compile(r"(?<![\w&])#\w+")
_MENTION_IN_TEXT = re.compile(r"(?<![\w.])@[A-Za-z0-9._]+")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def validate_alt_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > ALT_TEXT_MAX:
        raise ValidationError(f"alt_text must be text of at most {ALT_TEXT_MAX} characters")
    return value


def validate_user_tags(value: Any, *, require_xy: bool = True) -> str | None:
    """[{username, x, y}] -> JSON string for the Graph API. x/y are 0..1 (image only)."""
    import json

    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValidationError("user_tags must be a non-empty list of {username, x, y}")
    if len(value) > MAX_USER_TAGS:
        raise ValidationError(f"user_tags allows at most {MAX_USER_TAGS} people")
    out = []
    for tag in value:
        if not isinstance(tag, dict) or not _USERNAME_RE.match(str(tag.get("username", ""))):
            raise ValidationError("each user_tag needs a valid Instagram username")
        item: dict[str, Any] = {"username": tag["username"]}
        if require_xy or "x" in tag or "y" in tag:
            for axis in ("x", "y"):
                v = tag.get(axis)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                    raise ValidationError(f"user_tag {axis} must be a number between 0 and 1")
                item[axis] = v
        out.append(item)
    return json.dumps(out)


def validate_collaborators(value: Any) -> str | None:
    import json

    if value is None:
        return None
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list) or not value or len(value) > MAX_COLLABORATORS:
        raise ValidationError(f"collaborators must be 1-{MAX_COLLABORATORS} Instagram usernames")
    for u in value:
        if not isinstance(u, str) or not _USERNAME_RE.match(u.lstrip("@")):
            raise ValidationError(f"collaborator {u!r} is not a valid Instagram username")
    return json.dumps([u.lstrip("@") for u in value])


def validate_location_id(value: str | int | None) -> str | None:
    if value is None or value == "":
        return None
    v = str(value).strip()
    if not v.isdigit() or len(v) > 30:
        raise ValidationError("location_id must be a numeric Facebook location/Page id")
    return v


def validate_hashtag(value: str, *, field: str = "hashtag") -> str:
    """A hashtag query: leading '#' optional, alphanumeric/underscore, <=100 chars."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lstrip("#")
    if not _HASHTAG_RE.match(v):
        raise ValidationError(
            f"{field} must be alphanumeric/underscore, <=100 chars, no spaces (got {truncate(v, 40)!r})"
        )
    return v


def validate_username(value: str, *, field: str = "username") -> str:
    """An Instagram username for business_discovery: 1-30 chars, IG's charset."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9._]{1,30}$", v):
        raise ValidationError(
            f"{field} must be a valid Instagram username (<=30, letters/digits/._; got {truncate(v, 40)!r})"
        )
    return v


def validate_account_label(value: str, *, field: str = "account") -> str:
    """A human-friendly account label key (e.g. 'onde', 'mycelium', 'client-acme')."""
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _ACCOUNT_LABEL_RE.match(v):
        raise ValidationError(f"{field} must be 1-63 chars of letters/digits/._- (got {truncate(v, 40)!r})")
    return v


def validate_public_https_url(value: str, *, field: str = "media_url") -> str:
    """A media URL handed to Instagram for ingestion.

    Instagram (not this MCP) fetches the bytes, so the SSRF blast radius is on
    Meta's side; we still enforce https + reject obvious non-public hosts so a
    malformed/localhost URL fails fast at the INPUT rail instead of as an opaque
    container error. The authoritative SSRF guard for URLs WE fetch lives in
    graph_client.assert_safe_host().
    """
    if value is None or not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip()
    if not v.lower().startswith("https://"):
        raise ValidationError(f"{field} must be an https:// URL (Instagram refuses non-https media)")
    host = re.sub(r"^https://", "", v, flags=re.I).split("/", 1)[0].split(":", 1)[0].lower()
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        raise ValidationError(f"{field} host {host!r} is not publicly reachable; Instagram cannot fetch it")
    return v


_IG_CDN_HOSTS = ("cdninstagram.com", "fbcdn.net")


def validate_video_url(value: str, *, field: str = "video_url") -> str:
    """Public https video URL that Instagram will accept as an upload source."""
    v = validate_public_https_url(value, field=field)
    host = re.sub(r"^https://", "", v, flags=re.I).split("/", 1)[0].split(":", 1)[0].lower()
    if any(host == h or host.endswith("." + h) for h in _IG_CDN_HOSTS):
        raise ValidationError(
            f"{field} points to Instagram's own CDN ({host}); Meta rejects those as video sources "
            "(error 2207076). Upload the video file to your own website or storage and use that link."
        )
    return v


def validate_limit(
    value: int | str | None,
    *,
    field: str = "limit",
    default: int = 25,
    ceiling: int = 100,
    all_ceiling: int | None = None,
) -> int:
    """Item count: 1..ceiling, or "all" (-> all_ceiling, else ceiling). Default when None.

    Numeric strings ("40") are accepted because MCP clients sometimes send numbers as text.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError(f'{field} must be an integer or "all"')
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "all":
            return all_ceiling or ceiling
        if not v.isdigit():
            raise ValidationError(f'{field} must be an integer or "all"')
        value = int(v)
    if not isinstance(value, int):
        raise ValidationError(f'{field} must be an integer or "all"')
    if value < 1 or value > ceiling:
        raise ValidationError(f'{field} must be between 1 and {ceiling} (or "all")')
    return value


_CURSOR_RE = re.compile(r"^[0-9A-Za-z_\-=]{1,4096}$")


def validate_cursor(value: str | None, *, field: str = "after") -> str | None:
    """A Graph paging cursor (opaque base64-ish). None passes through."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _CURSOR_RE.match(value.strip()):
        raise ValidationError(f"{field} must be the next_cursor value returned by a previous call")
    return value.strip()


_UNTIL_CURSOR_RE = re.compile(r"^until-(\d{9,11})(?:-(\d{1,30}))?$")


def parse_until_cursor(value: str | None) -> tuple[int, str | None] | None:
    """(epoch, last_id) from a synthetic 'until-<epoch>[-<id>]' cursor, else None."""
    m = _UNTIL_CURSOR_RE.match(value or "")
    return (int(m.group(1)), m.group(2)) if m else None


def validate_date(value: str | None, *, field: str = "date", end_of_day: bool = False):
    """YYYY-MM-DD or ISO-8601 -> aware UTC datetime. Date-only + end_of_day -> 23:59:59."""
    from datetime import datetime, time, timezone

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a date string like 2026-11-01")
    v = value.strip()
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) or re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", v)):
        raise ValidationError(
            f"{field} must be a date like 2026-11-01 or a full ISO date-time (got {truncate(v, 30)!r})"
        )
    try:
        if len(v) == 10:
            d = datetime.strptime(v, "%Y-%m-%d").date()
            return datetime.combine(d, time.max if end_of_day else time.min, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be a date like 2026-11-01 (got {truncate(v, 30)!r})") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_graph_time(value: str):
    """Graph timestamps look like 2026-11-11T18:21:43+0000."""
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def validate_enum(value: str | None, allowed: set[str], *, field: str, default: str | None = None) -> str | None:
    """Validate a string against a fixed allow-list (e.g. media_type, metric period)."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    v = value.strip().lower()
    if v not in allowed:
        raise ValidationError(f"{field} must be one of {sorted(allowed)} (got {truncate(v, 40)!r})")
    return v
