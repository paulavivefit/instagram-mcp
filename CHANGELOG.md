# Changelog

All notable changes to instagram-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

## [0.2.0] — 2026-11-11

Fork release (paulavivefit/instagram-mcp). 35 tools, 117+ tests with an in-memory fake Graph API.

### Fixed
- `get_account_insights` default failed: retired `impressions` removed; totals (`views`, `profile_views`, `accounts_engaged`, `total_interactions`) requested with `metric_type=total_value` (period=day only).
- `get_audience_insights` default failed: one request per breakdown (Meta allows one).
- DM tools: Page access token on the Page node; one conversation per request; long base64 conversation ids accepted; `get_messages` respects `limit`.

### Added
- `limit="all"` with per-tool ceilings, `next_cursor`/`after` paging, `list_media` `since`/`until`.
- Insights over any date range (30-day windows merged), `top_posts`, `insights_summary`, per-type media metrics.
- `get_comments(include_replies=True)`, `get_comment_replies`, carousel children in `get_media`, `list_stories`.
- Publishing: alt text, user tags, location, collaborators, reel thumb offset/audio name, mixed image+video carousels, caption hashtag/mention limits, `publish_container` to resume a slow video.
- DMs: `HUMAN_AGENT` tag, image attachments, `mark_seen`/typing, attachments/shares/story replies in `get_messages`, `outside_window` error with hint.
- Retries with exponential backoff (never repeats a timed-out POST), Retry-After, rate-limit usage in `healthcheck`, 60 s timeout, 300 s video wait.
- `scripts/smoke_readonly.py`: live read-only check of every read tool.

### Changed
- Default Graph API version v21.0 → v27.0 (all read tools verified on both).

## [0.1.2] — 2026-06-17

### Added
- `mcp-name` marker in the README so the MCP registry can validate PyPI package ownership (`io.github.adelaidasofia/instagram-mcp`). No code or tool-surface change.

## [0.1.1] — 2026-06-17

### Added
- `docs/APP_REVIEW.md` — full Meta App Review walkthrough to unlock the DM tools: login-path note, business-verification + privacy-policy requirements, screencast shot list, reviewer test-instructions template, post-approval flip, and the 24h-window / message-tag caveat. Linked from README + SETUP.
- PyPI distribution as `adelaidasofia-instagram-mcp` (the bare `instagram-mcp` name was already taken on PyPI): `.github/workflows/publish-mcp.yml` release workflow (OIDC trusted publisher, pinned action SHAs, hardened build/publish job split) plus an `instagram-mcp` console-script entry point.
- `server.json` describing the server for the MCP registry (`io.github.adelaidasofia/instagram-mcp`).

### Changed
- Repackaged the flat top-level modules (`server`, `auth`, `audit`, `graph_client`, `validators`) under the `instagram_mcp` package so the published wheel no longer squats generic top-level import names. The MCP tool surface is unchanged.

## [0.1.0] — 2026-06-16

Initial release.

### Added
- 29 tools over the official Instagram Graph API:
  - **Accounts & health** (6): healthcheck, list_accounts, add_account, set_default_account, remove_account, account_info.
  - **Profile & media** (3): get_profile, list_media, get_media.
  - **Insights** (3): get_account_insights, get_media_insights, get_audience_insights.
  - **Publishing** (6): publish_image, publish_video, publish_reel, publish_carousel, publish_story, publishing_limit — with async container-status polling for video/reel/story.
  - **Comments** (4): get_comments, reply_to_comment, hide_comment, delete_comment.
  - **Discovery** (4): search_hashtag, get_hashtag_media, get_mentions, business_discovery.
  - **Direct messages** (3): list_conversations, get_messages, send_message — gated behind Meta App Review (`instagram_manage_messages`), fail-loud until `INSTAGRAM_MCP_DM_ENABLED=1`.
- Multi-account support with OS-keychain token storage (chmod-600 file fallback); tokens never echoed or logged.
- Safety: Meta-host egress allow-list + fail-closed SSRF guard; `sanitize_error` scrubber (Meta `EAA…`/`IGQV…` tokens, bearer headers, app secrets, API keys); 4-field JSONL audit; INPUT-rail validation.

[0.1.2]: https://github.com/adelaidasofia/instagram-mcp/releases/tag/v0.1.2
[0.1.1]: https://github.com/adelaidasofia/instagram-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/adelaidasofia/instagram-mcp/releases/tag/v0.1.0
