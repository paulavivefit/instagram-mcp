# instagram-mcp

<!-- mcp-name: io.github.adelaidasofia/instagram-mcp -->

An [MCP](https://modelcontextprotocol.io) server for the **official Instagram Graph API**. Read, publish, comment, and pull analytics across one or many Instagram **Business/Creator** accounts from Claude (or any MCP client) — ToS-safe, no private/reverse-engineered API.

35 tools across accounts, media, publishing, insights, comments, discovery, and (review-gated) direct messages.

```
npx-style stdio MCP · Python · FastMCP · MIT
```

## Why this one

Most "Instagram automation" tools either (a) wrap the **unofficial** private API (username + password) — which violates Instagram's Terms and risks a ban — or (b) only **post**, with no way to read insights, comments, or mentions back. This server is built entirely on the **official Graph API**, is **multi-account** from day one, and covers the full read + write surface. The only thing it cannot do without Meta's approval is DMs (see [Direct messages](#direct-messages)).

## Requirements

- An Instagram **Professional account** (Business or Creator). Personal accounts cannot use the Graph API. Converting is free and reversible (Instagram app → Settings → Account type).
- The Instagram account connected to a Facebook Page.
- A long-lived **access token** with `instagram_basic` + `instagram_content_publish` + `instagram_manage_comments` + `instagram_manage_insights`, and the account's numeric **Instagram Business Account id**. See [SETUP.md](SETUP.md) for the exact token + id steps.
- Python 3.10+.

## Install

From PyPI:

```bash
pip install adelaidasofia-instagram-mcp
```

Or from source:

```bash
git clone https://github.com/adelaidasofia/instagram-mcp
cd instagram-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```

Register it with your MCP client (Claude Desktop / Claude Code) — single-account zero-config path:

```json
{
  "mcpServers": {
    "instagram": {
      "command": "instagram-mcp",
      "env": {
        "INSTAGRAM_MCP_ACCESS_TOKEN": "EAA...your-long-lived-token...",
        "INSTAGRAM_MCP_IG_USER_ID": "17841400000000000"
      }
    }
  }
}
```

`instagram-mcp` is the console script the package installs. From a source checkout it lives at `.venv/bin/instagram-mcp`, or run the module directly with `python -m instagram_mcp.server`.

Or skip the env vars and call `add_account` at runtime (token goes to your OS keychain). See [Multiple accounts](#multiple-accounts).

## Tools

**Accounts & health** — `healthcheck` (incl. Meta rate-limit usage), `list_accounts`, `add_account`, `set_default_account`, `remove_account`, `account_info`

**Profile & media** — `get_profile`, `list_media` (`limit` up to 10 000 or `"all"`, `since`/`until` dates, `next_cursor` paging), `get_media` (incl. carousel children), `list_stories` (active stories, optional insights)

**Insights** — `get_account_insights` (reach, views, profile views, accounts engaged, interactions, follower count; any date range, split into Meta's 30-day windows), `get_media_insights` (right metrics per feed post / reel / story), `get_audience_insights` (age, gender, country, city), `top_posts` (rank posts in a range by reach/views/likes/…, batched), `insights_summary` (last 7/30/90 days vs the period before)

**Publishing** — `publish_image` (alt text, user tags, location, collaborators), `publish_video`, `publish_reel` (cover/thumb offset, collaborators, audio name, location), `publish_carousel` (2–10 images and/or videos), `publish_story`, `publish_container` (finish a video that was still processing), `publishing_limit`. Captions are checked for Instagram's limits (2200 chars, 30 hashtags, 20 mentions) before anything is sent.

**Comments** — `get_comments` (`"all"`, optional replies), `get_comment_replies`, `reply_to_comment`, `hide_comment`, `delete_comment`

**Discovery** — `search_hashtag`, `get_hashtag_media`, `get_mentions`, `business_discovery` (any public Professional account, up to 500 posts)

**Direct messages** — `list_conversations`, `get_messages` (text, attachments, shares, story replies), `send_message` (text and/or image, optional `HUMAN_AGENT` tag), `mark_seen` (see below)

**Reliability** — automatic retries with backoff for slow or rate-limited Meta responses (a timed-out publish or send is never repeated), 60 s request timeout, 5 min video processing wait. Knobs: `INSTAGRAM_MCP_TIMEOUT`, `INSTAGRAM_MCP_MAX_ATTEMPTS`, `INSTAGRAM_MCP_VIDEO_WAIT_S`, `INSTAGRAM_MCP_GRAPH_VERSION` (default `v27.0`).

**Review before posting** — every `publish_*` tool takes `prepare_only=true`: Instagram downloads and processes the post (and validates tags, collaborators, location) but nothing is published; call `publish_container(container_id)` within ~24 h to publish it.

**Honest numbers** — insights come back flat (`metrics: {views: 202790, reach: [{date, value}, ...]}`; `raw=true` for Meta's original). Dates are **Meta's Pacific-time days** (a value stamped 08:00 UTC belongs to the previous Pacific day); ranges are split into Meta's 30-day windows on Pacific-day boundaries with no day counted twice. When Meta has no real value for a period (e.g. account `views` before mid-2025, or 0/negative numbers next to real activity) the metric is `null` and listed in `unavailable`, never a fake 0. Old reels and pre-2023 posts get a `warnings` entry because Meta under-reports their reach. `healthcheck` shows token scopes/expiry and which features actually work (DMs in development mode, hashtag access).

**DMs before App Review** — Meta only lets the app read/send DMs with people who have a role on the app, and a large inbox makes conversation listing time out in development mode; pass `user_id` to `list_conversations` to fetch one person's conversation. There is no Meta API to look up a DM id (IGSID) from a username; it only comes from conversations or webhooks.

**Known Meta limits (verified live)** — plain feed videos are no longer accepted, so `publish_video` publishes a reel shown in the grid; video links from Instagram's own CDN (cdninstagram.com / fbcdn.net) are rejected as sources; tagged users and collaborators must be public accounts; `location_id` must be a Facebook Page with an address; hashtag search needs Meta's *Instagram Public Content Access* feature (App Review). The API cannot delete posts.

> Publishing takes **public https media URLs** — Instagram fetches the bytes itself, so the image/video must be reachable on the open web (an S3/Cloudflare/any-CDN URL works).

## Multiple accounts

Run one server for all your accounts (yours, a brand's, a client's). Each account authorizes its own token:

```
add_account(label="brand-a", access_token="EAA...", ig_user_id="178414...", make_default=True)
add_account(label="brand-b", access_token="EAA...", ig_user_id="178414...")
list_media(account="brand-b")
get_account_insights(account="brand-a")
```

Tokens are stored in the **macOS keychain** (or a `chmod 600` file on other platforms), never in the metadata file and never returned by any tool. Omit `account` on any tool to use the default.

## Direct messages

The DM tools require the `instagram_manage_messages` permission, which Meta grants **only through App Review** (typically weeks, and stricter in 2025–2026). Until then, the DM tools fail loud with that instruction rather than silently no-op. Once your app is approved and the token carries the scope, set `INSTAGRAM_MCP_DM_ENABLED=1`. Note Instagram's 24-hour standard-messaging window applies; up to 7 days a human-written reply can be sent with `tag="HUMAN_AGENT"` (needs the `human_agent` permission). DM calls use the linked Facebook Page's token automatically (the user token returns an opaque error for conversations).

Full App Review walkthrough (screencast shot list, reviewer test-instructions template, privacy-policy + business-verification requirements, post-approval flip, message-tag caveat): **[docs/APP_REVIEW.md](docs/APP_REVIEW.md)**.

## Safety

- **Egress is pinned** to the Meta host allow-list (`graph.facebook.com` / `graph.instagram.com`). A general SSRF guard (RFC1918 / loopback / link-local / CGNAT / cloud-metadata, fail-closed DNS) backs any URL handling.
- **Credentials never leak**: every result and error passes a scrubber that strips access tokens (incl. Meta `EAA…` / `IGQV…`), bearer headers, app secrets, and API keys before it reaches the model.
- **Observability**: every call appends a 4-field JSONL audit line (`execution_time_ms`, `io`, `token_usage`, `error_class`) under `~/.claude/instagram-mcp/audit.log.jsonl`.
- **Input validation** runs before every Graph call (ids, caption length, hashtag/username charset, https media URLs).

## License

MIT — see [LICENSE](LICENSE).

Built by Adelaida Diaz-Roa. Full install or team version at [diazroa.com](https://diazroa.com).
