# Changelog

All notable changes to instagram-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is [SemVer](https://semver.org/).

## [0.3.0] — 2026-11-13

Driven by real use in Claude Desktop plus three independent reviews (correctness, performance, security). 34 tools, 311 tests, 97 % line coverage; every stage verified against the live Graph API.

### Changed (output shapes)
- Insights are flat: `metrics: {name: total | [{date, value}]}`, Meta's localized titles dropped (`raw=true` keeps them). Audience breakdowns sorted with `percent`. Replies and carousel children are plain lists.
- `account_info` removed (duplicate of `get_profile`).
- Hourly Meta rate limits (codes 4/17/32/80002) fail fast with a usage hint instead of being retried.

### Fixed (wrong or misleading data)
- Daily values carry Meta's Pacific `date`; windows aligned to Pacific midnight; Meta includes both since/until edges, so requests start 1 s after midnight (exact day counts, no duplicates); 29-day windows across the autumn DST change.
- Metrics Meta did not collect (0/negative next to activity, e.g. account views before mid-2025) are `null` + `unavailable`, never 0; under-reported reach on old reels/posts gets `warnings`.
- `insights_summary` periods no longer overlap; % change null when data is incomplete.
- `paginate` never exceeds the limit; adding/removing accounts never silently moves the default; only real token errors are `oauth`; PUBLISHED/EXPIRED containers handled; wrapped errors keep Meta's code and hint; strict dates; non-JSON responses are errors.

### Added
- `compact=true`, synthetic `next_cursor` for date-filtered pages, `top_posts` by likes/comments for any age, `healthcheck` token + feature status, `list_conversations(user_id=...)` with a time budget, clear hints for development-mode DMs, hashtag access, untaggable users, old posts, the 2-year limit.
- Security hardening (Bearer header, keychain via stdin, private atomic files, audit rotation, sanitized extras, duplicate-write warnings, third-party-text notes), pinned dependencies with `uv.lock`.

## [0.2.1] — 2026-11-12

### Added
- `prepare_only` on every publish tool (process at Instagram without publishing; finish with `publish_container`).
- Parameter descriptions, enum choices and MCP safety annotations (title/readOnly/destructive/openWorld) on all 35 tools.
- `scripts/e2e_live.py`: real-API end-to-end test of every tool over the MCP protocol (public phases behind explicit flags).

### Fixed (found by the real-API e2e run)
- `publish_video`: Meta rejects media_type=VIDEO for feed posts; now published as a reel shown in the grid.
- "Invalid user id" (code 110) for untaggable usernames is a parameter error with a hint, not an OAuth error.
- Clear hints for unusable video sources (Instagram CDN links rejected up front; error 2207076), location pages without an address (2207019), and hashtag search needing Instagram Public Content Access.

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
