"""Stage 10: the tool surface Claude sees must be clean, descriptive and safe."""

from __future__ import annotations

import asyncio

import pytest

READ_ONLY = {
    "healthcheck", "list_accounts", "get_profile", "list_media", "get_media",
    "list_stories", "get_account_insights", "get_media_insights", "get_audience_insights",
    "top_posts", "insights_summary", "publishing_limit", "get_comments", "get_comment_replies",
    "search_hashtag", "get_hashtag_media", "get_mentions", "business_discovery",
    "list_conversations", "get_messages",
}
DESTRUCTIVE = {"delete_comment", "remove_account"}
PUBLIC_WRITES = {"publish_image", "publish_video", "publish_reel", "publish_carousel", "publish_story",
                 "publish_container", "reply_to_comment", "hide_comment", "send_message", "mark_seen"}
LOCAL_WRITES = {"add_account", "set_default_account"}

ENUMS = {
    ("get_account_insights", "period"): {"day", "week", "days_28"},
    ("get_audience_insights", "metric"): {"follower_demographics", "engaged_audience_demographics"},
    ("get_audience_insights", "timeframe"): {"this_week", "this_month"},
    ("get_hashtag_media", "edge"): {"top_media", "recent_media"},
    ("top_posts", "metric"): {"reach", "views", "likes", "comments", "shares", "saved", "total_interactions"},
    ("top_posts", "media_type"): {"REELS", "FEED", "IMAGE", "VIDEO", "CAROUSEL_ALBUM"},
    ("insights_summary", "period"): {"last_7_days", "last_30_days", "last_90_days"},
    ("mark_seen", "action"): {"mark_seen", "typing_on", "typing_off"},
    ("send_message", "tag"): {"HUMAN_AGENT"},
}


@pytest.fixture(scope="module")
def tools():
    from instagram_mcp import server
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _enum_values(schema):
    for alt in [schema, *schema.get("anyOf", [])]:
        if "enum" in alt:
            return set(alt["enum"])
        if "const" in alt:
            return {alt["const"]}
    return None


def test_every_tool_is_classified(tools):
    assert set(tools) == READ_ONLY | DESTRUCTIVE | PUBLIC_WRITES | LOCAL_WRITES


def test_every_parameter_is_described(tools):
    missing = [f"{name}.{p}" for name, t in tools.items()
               for p, schema in t.parameters.get("properties", {}).items()
               if len((schema.get("description") or "").strip()) < 15]
    assert missing == []


def test_tool_descriptions_are_concise_and_lead_with_purpose(tools):
    for name, t in tools.items():
        first = (t.description or "").strip().splitlines()[0]
        assert 20 <= len(first) <= 110, (name, first)
        assert len(t.description) <= 900, name


def test_safety_annotations(tools):
    for name in READ_ONLY:
        a = tools[name].annotations
        assert a is not None and a.read_only_hint is True, name
    for name in DESTRUCTIVE:
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is True, name
    for name in PUBLIC_WRITES | LOCAL_WRITES:
        a = tools[name].annotations
        assert a.read_only_hint is False and a.destructive_hint is False, name
    for name in PUBLIC_WRITES:
        assert "public" in tools[name].description.lower() or "customer" in tools[name].description.lower(), name


def test_fixed_choices_are_enums(tools):
    for (tool, param), expected in ENUMS.items():
        schema = tools[tool].parameters["properties"][param]
        assert _enum_values(schema) == expected, (tool, param, schema)


def test_no_internal_or_stale_text(tools):
    blob = " ".join((t.description or "") + str(t.parameters) for t in tools.values())
    for stale in ("90s", "onde", "mycelium", "client-acme"):
        assert stale not in blob, stale
    assert "metric_type" not in tools["get_audience_insights"].parameters["properties"]
    assert "period" not in tools["get_audience_insights"].parameters["properties"]


def test_tool_titles(tools):
    for name, t in tools.items():
        assert t.annotations.title, name
