"""The facebook-mcp tool surface: classified, described, annotated."""

from __future__ import annotations

import asyncio

import pytest

READ_ONLY = {
    "healthcheck", "list_pages", "get_page", "list_posts", "get_post", "top_posts", "list_scheduled_posts",
    "list_tagged_posts", "get_comments", "get_comment", "get_comment_replies", "get_page_insights",
    "get_post_insights", "insights_summary",
}
DESTRUCTIVE = {"delete_post", "delete_comment"}
PUBLIC_WRITES = {"create_post", "create_photo_post", "create_video_post", "update_post", "comment_on_post",
                 "reply_to_comment", "hide_comment", "unhide_comment"}


@pytest.fixture(scope="module")
def tools():
    from facebook_mcp import server
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_every_tool_classified(tools):
    assert set(tools) == READ_ONLY | DESTRUCTIVE | PUBLIC_WRITES


def test_annotations_match_class(tools):
    for name, t in tools.items():
        a = t.annotations
        assert a.readOnlyHint is (name in READ_ONLY), name
        if name in DESTRUCTIVE:
            assert a.destructiveHint is True, name
        if name in PUBLIC_WRITES:
            assert a.destructiveHint is False, name
        assert a.title, name


def test_every_parameter_described(tools):
    missing = [f"{n}.{p}" for n, t in tools.items() for p, s in t.parameters.get("properties", {}).items()
               if len((s.get("description") or "").strip()) < 15]
    assert missing == []


def test_descriptions_concise(tools):
    for name, t in tools.items():
        first = (t.description or "").strip().splitlines()[0]
        assert 20 <= len(first) <= 160, name


def test_server_name(tools):
    from facebook_mcp import server
    assert server.mcp.name == "facebook-mcp"
