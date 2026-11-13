"""SSRF-safe Instagram Graph API client for instagram-mcp.

Two layers of egress safety:
  1. The client only ever talks to a FIXED allow-list of Meta hosts
     (graph.facebook.com / graph.instagram.com). An allow-list beats a blocklist
     for a fixed-upstream client — there is no code path that fetches a
     user-supplied host. (Media URLs handed to publish_* are fetched by
     Instagram, not by us.)
  2. assert_safe_host() is the general RFC1918/loopback/link-local/CGNAT/metadata
     blocklist (fail-closed DNS) per url-input-safety.md, exercised by the
     negative-control test and available if a future tool ever fetches a user URL.

Graph API errors are mapped to a stable `error_class` (oauth / rate_limited /
permission / not_found / invalid_param / ssrf_blocked / upstream_error) so the
audit taxonomy and the tool callers can branch without string-matching Meta's
prose. The raw token is never echoed: GraphAPIError messages pass through
audit.sanitize_error at the seam.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import random
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx

GRAPH_VERSION = os.environ.get("INSTAGRAM_MCP_GRAPH_VERSION", "v27.0")
DEFAULT_TIMEOUT = float(os.environ.get("INSTAGRAM_MCP_TIMEOUT", "60"))
MAX_ATTEMPTS = max(1, int(os.environ.get("INSTAGRAM_MCP_MAX_ATTEMPTS", "3")))
BACKOFF_BASE_S = 1.0
MAX_RETRY_AFTER_S = 60.0

# Meta "too many calls" codes: the request was rejected unprocessed, so even a POST
# can be retried safely. (4 app, 17 user, 32 page, 613 custom, 80002 Instagram BUC.)
THROTTLE_CODES = frozenset({4, 17, 32, 613, 80002})

_sleep = time.sleep  # patched in tests

# Latest rate-limit usage reported by Meta response headers (percent of quota).
LAST_USAGE: dict[str, Any] = {}

# Test seam: when set (tests only), every GraphClient sends through this transport
# instead of the network. Production leaves it None.
DEFAULT_TRANSPORT: httpx.BaseTransport | None = None

# The ONLY hosts this client will ever contact.
ALLOWED_HOSTS: frozenset[str] = frozenset({"graph.facebook.com", "graph.instagram.com"})

# Cloud-metadata endpoints that are not caught by the private/link-local checks
# on every platform.
_METADATA_IPS: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class GraphAPIError(RuntimeError):
    """A Graph API (or egress-safety) failure with a stable error_class."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "upstream_error",
        code: int | None = None,
        subcode: int | None = None,
        type_: str | None = None,
        http_status: int | None = None,
        transient: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.extra = extra or {}
        self.error_class = error_class
        self.code = code
        self.subcode = subcode
        self.type = type_
        self.http_status = http_status


def _ip_is_blocked(ip_str: str) -> bool:
    """True if an IP is private / loopback / link-local / reserved / CGNAT / metadata."""
    addr = ipaddress.ip_address(ip_str)
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True
    if addr.version == 4 and addr in _CGNAT:
        return True
    return str(addr) in _METADATA_IPS


def assert_safe_host(url: str) -> str:
    """Resolve the URL's host and refuse any non-public address. Fail-closed DNS.

    Returns the hostname on success; raises GraphAPIError(error_class='ssrf_blocked'
    | 'validation' | 'network') otherwise. This is the general guard for arbitrary
    URLs — the GraphClient itself additionally pins to ALLOWED_HOSTS.
    """
    host = urlparse(url).hostname
    if not host:
        raise GraphAPIError(f"URL has no host: {url!r}", error_class="validation")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        # Fail closed: an unresolvable host is refused, never optimistically fetched.
        raise GraphAPIError(f"DNS resolution failed for host {host!r}", error_class="network") from exc
    for info in infos:
        ip_str = info[4][0]
        if _ip_is_blocked(ip_str):
            raise GraphAPIError(
                f"host {host!r} resolves to a non-public address ({ip_str}) — refused",
                error_class="ssrf_blocked",
            )
    return host


def _map_graph_error(payload: dict[str, Any], http_status: int) -> GraphAPIError:
    """Translate a Graph API error envelope into a typed GraphAPIError."""
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = err.get("message") or f"Graph API error (HTTP {http_status})"
    code = err.get("code")
    subcode = err.get("error_subcode")
    etype = err.get("type")

    error_class = "upstream_error"
    if code == 190 or etype == "OAuthException":
        error_class = "oauth"
    if code in THROTTLE_CODES or (isinstance(message, str) and "rate limit" in message.lower()):
        error_class = "rate_limited"
    if code in {10, 200, 803} or (isinstance(code, int) and 200 <= code <= 299):
        error_class = "permission"
    if code == 100:
        # 100 is "invalid parameter" UNLESS it's the missing-permission subcode family.
        error_class = "invalid_param"
    if subcode == 2534084 or (code == -2 and str(message).strip().lower() == "timeout"):
        # Development mode: Meta must skip conversations with people who have no app role,
        # and a large inbox makes that query time out. Not a token problem.
        error_class = "dev_mode_limit"
    if code == 110:
        # "Invalid user id": a bad username in user_tags/collaborators, not a login problem
        error_class = "invalid_param"
    if http_status == 404:
        error_class = "not_found"

    return GraphAPIError(
        message if isinstance(message, str) else str(message),
        error_class=error_class,
        code=code if isinstance(code, int) else None,
        subcode=subcode if isinstance(subcode, int) else None,
        type_=etype if isinstance(etype, str) else None,
        http_status=http_status,
        transient=bool(err.get("is_transient")),
        extra=_error_hint(subcode if isinstance(subcode, int) else None, message),
    )


ERROR_HINTS_BY_SUBCODE = {
    2207018: "Instagram could not find that user. People in user_tags and collaborators must be existing "
    "PUBLIC Instagram accounts (private accounts cannot be tagged via the API); check the spelling.",
    2534084: "Meta timed out listing conversations: in development mode it has to skip every "
    "conversation with people who have no role on the app, and this inbox is too large. "
    "Pass user_id (the person's Instagram-scoped id) to list_conversations to fetch one "
    "conversation directly, or wait for App Review approval (Live mode), which removes the limit.",
    2108006: "Meta has no insights for this post: it is older than the account's switch to a "
    "Business/Creator account (or otherwise too old to be covered).",
    2207013: "No Business/Creator account with that username: it may not exist, be misspelled, "
    "or be a personal/private account (business_discovery only works for Professional accounts).",
    2207019: "location_id must be the id of a Facebook Page that has a physical address set (a place). "
    "Pages without an address are rejected.",
    2207067: "Meta rejected a parameter for this media type (for example media_type=VIDEO for feed posts, "
    "which Meta no longer accepts; videos are published as reels).",
}
PUBLIC_CONTENT_HINT = (
    "This endpoint needs Meta's 'Instagram Public Content Access' feature, which must be "
    "requested in App Review (developers.facebook.com -> your app -> App Review -> "
    "Permissions and Features). Until it is approved, hashtag search cannot be used."
)


def _error_hint(subcode: int | None, message: Any) -> dict[str, Any] | None:
    if subcode in ERROR_HINTS_BY_SUBCODE:
        return {"hint": ERROR_HINTS_BY_SUBCODE[subcode]}
    if isinstance(message, str) and "Instagram Public Content Access" in message:
        return {"hint": PUBLIC_CONTENT_HINT}
    return None


def _backoff(attempt: int) -> float:
    """Exponential backoff with a little jitter: ~1s, ~2s, ~4s ..."""
    return BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.25)


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return min(float(raw), MAX_RETRY_AFTER_S) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _pct_max(values: Any) -> float:
    nums = [v for v in (values or []) if isinstance(v, (int, float))]
    return float(max(nums)) if nums else 0.0


def _record_usage(headers: Any) -> None:
    """Remember Meta's rate-limit usage headers (percent of the hourly quota)."""
    import json

    if headers is None:
        return
    app_raw = headers.get("X-App-Usage")
    buc_raw = headers.get("X-Business-Use-Case-Usage")
    if not app_raw and not buc_raw:
        return
    usage: dict[str, Any] = {}
    peak = 0.0
    try:
        if app_raw:
            app = json.loads(app_raw)
            usage["app"] = app
            peak = max(peak, _pct_max([app.get("call_count"), app.get("total_time"), app.get("total_cputime")]))
        if buc_raw:
            buc = json.loads(buc_raw)
            usage["business"] = buc
            for entries in buc.values():
                for e in entries or []:
                    peak = max(peak, _pct_max([e.get("call_count"), e.get("total_time"), e.get("total_cputime")]))
    except (ValueError, AttributeError, TypeError):
        return
    usage["max_percent"] = int(peak)
    usage["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    LAST_USAGE.clear()
    LAST_USAGE.update(usage)


class GraphClient:
    """Thin, SSRF-pinned wrapper over the Instagram Graph API.

    One client == one account token. Construct via auth.client_for(account) so the
    token is resolved from the keychain/env, never passed around by tool callers.
    """

    def __init__(
        self,
        access_token: str,
        *,
        base_host: str = "graph.facebook.com",
        app_secret: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if base_host not in ALLOWED_HOSTS:
            raise GraphAPIError(
                f"base_host {base_host!r} is not in the Meta allow-list {sorted(ALLOWED_HOSTS)}",
                error_class="ssrf_blocked",
            )
        if not access_token:
            raise GraphAPIError("missing access token", error_class="oauth")
        self._token = access_token
        self._base = base_host
        self._app_secret = app_secret
        self._timeout = timeout
        self._transport = transport
        self._max_attempts = max_attempts
        self.last_cut = False

    def _appsecret_proof(self) -> str | None:
        """HMAC-SHA256(token) keyed by app_secret — Meta's recommended call hardening."""
        if not self._app_secret:
            return None
        return hmac.new(
            self._app_secret.encode("utf-8"),
            self._token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_params(self) -> dict[str, str]:
        params = {"access_token": self._token}
        proof = self._appsecret_proof()
        if proof:
            params["appsecret_proof"] = proof
        return params

    def _url(self, path: str) -> str:
        return f"https://{self._base}/{GRAPH_VERSION}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        # Defense in depth: even though path is internal, confirm the base host
        # never drifted out of the allow-list.
        if urlparse(url).hostname not in ALLOWED_HOSTS:
            raise GraphAPIError(f"refusing egress to non-Meta host in {url!r}", error_class="ssrf_blocked")
        query = dict(params or {})
        query.update(self._auth_params())
        attempt = 0
        while True:
            attempt += 1
            can_retry = attempt < (self._max_attempts or MAX_ATTEMPTS)
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport or DEFAULT_TRANSPORT) as client:
                    resp = client.request(method, url, params=query, data=data)
            except httpx.TimeoutException as exc:
                # A timed-out POST may have been processed (published/sent): never repeat it.
                if method == "GET" and can_retry:
                    _sleep(_backoff(attempt))
                    continue
                raise GraphAPIError(
                    f"Graph API request timed out after {self._timeout}s", error_class="timeout"
                ) from exc
            except httpx.HTTPError as exc:
                # A connect error means nothing was sent, so any method is safe to retry.
                if can_retry and (method == "GET" or isinstance(exc, httpx.ConnectError)):
                    _sleep(_backoff(attempt))
                    continue
                raise GraphAPIError(f"Graph API transport error: {exc}", error_class="network") from exc

            _record_usage(getattr(resp, "headers", None))
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
                err = _map_graph_error(body if isinstance(body, dict) else {}, resp.status_code)
                throttled = err.code in THROTTLE_CODES or resp.status_code == 429
                server_side = resp.status_code >= 500 or err.transient
                if can_retry and (throttled or (method == "GET" and server_side)):
                    _sleep(_retry_after(getattr(resp, "headers", None)) or _backoff(attempt))
                    continue
                raise err
            return body if isinstance(body, dict) else {"data": body}

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", path, params=clean)

    def post(self, path: str, **data: Any) -> dict[str, Any]:
        clean = {k: v for k, v in data.items() if v is not None}
        return self._request("POST", path, data=clean)

    def delete(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("DELETE", path, params=clean)

    def paginate(
        self,
        path: str,
        *,
        max_items: int,
        after: str | None = None,
        page_size: int = 100,
        stop: Any = None,
        skip: Any = None,
        **params: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk cursor pages until max_items, the end, or stop(item) is true.

        skip(item) -> True drops an item without counting it toward max_items.
        Returns (items, next_cursor). next_cursor is None when nothing is left (or the
        walk was stopped by `stop`); otherwise pass it back as `after` to resume.
        A page is never cut in half (Meta's cursor points past the whole page), so if
        Meta returns more than asked the result can slightly exceed max_items rather
        than silently dropping items.
        """
        out: list[dict[str, Any]] = []
        cursor = after
        self.last_cut = False
        while True:
            # While skipping, ask for full pages: skipped items don't count, and small
            # pages would burn through the hourly API budget.
            size = page_size if skip is not None else max(1, min(page_size, max_items - len(out)))
            page = self.get(path, limit=size, after=cursor, **params)
            data = page.get("data") or []
            for item in data:
                if stop is not None and stop(item):
                    return out, None
                if skip is not None and skip(item):
                    continue
                out.append(item)
            paging = page.get("paging") or {}
            nxt = (paging.get("cursors") or {}).get("after")
            if skip is not None and len(out) > max_items:
                # cut inside a page: a Graph cursor cannot resume mid-page
                self.last_cut = True
                return out[:max_items], None
            if not data or not nxt or not paging.get("next"):
                return out, None
            if len(out) >= max_items:
                return out, nxt
            cursor = nxt

    def get_paginated(
        self,
        path: str,
        *,
        limit: int = 25,
        max_items: int = 200,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Follow `paging.cursors`/`paging.next` until max_items collected.

        max_items is a hard ceiling so a tool can never walk an unbounded feed.
        """
        out: list[dict[str, Any]] = []
        page = self.get(path, limit=min(limit, max_items), **params)
        while True:
            data = page.get("data") or []
            out.extend(data)
            if len(out) >= max_items:
                return out[:max_items]
            after = (page.get("paging") or {}).get("cursors", {}).get("after")
            has_next = (page.get("paging") or {}).get("next")
            if not after or not has_next:
                return out
            page = self.get(path, limit=min(limit, max_items - len(out)), after=after, **params)
