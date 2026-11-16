"""Page resolution + the Page lock.

The user token can manage several Pages. This server may only act on Pages in the
allow-list (env FACEBOOK_MCP_ALLOWED_PAGE_IDS, comma separated). When the variable is
missing, only the Page linked to the default account's Instagram id is allowed:
never "every Page on the token".

Page tokens of Pages outside the allow-list are dropped right after me/accounts and are
never used, so no request is ever made on behalf of a forbidden Page.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from instagram_mcp import auth, graph_client
from instagram_mcp import validators as V

ALLOWED_ENV = "FACEBOOK_MCP_ALLOWED_PAGE_IDS"
_PAGE_ID_RE = re.compile(r"^\d{1,32}$")


@dataclass(frozen=True)
class Page:
    id: str
    name: str
    client: graph_client.GraphClient  # authenticated with the PAGE token
    account: str  # account label the user token came from
    tasks: tuple[str, ...] = ()
    ig_user_id: str | None = None

    def who(self) -> dict[str, Any]:
        return {"page_id": self.id, "page_name": self.name}


_CACHE: dict[tuple[str, str, str], dict[str, Page]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def forbidden(page_id: str) -> graph_client.GraphAPIError:
    return graph_client.GraphAPIError(
        f"Page {page_id} is not allowed for this connector.",
        error_class="forbidden_page",
        extra={"hint": f"Only Pages listed in {ALLOWED_ENV} can be used. Call list_pages to see them."},
    )


def allowed_ids_from_env() -> set[str] | None:
    raw = os.environ.get(ALLOWED_ENV)
    if raw is None or not raw.strip():
        return None
    ids = {x.strip() for x in raw.split(",") if x.strip()}
    bad = [x for x in ids if not _PAGE_ID_RE.match(x)]
    if bad:
        raise graph_client.GraphAPIError(
            f"{ALLOWED_ENV} must be numeric Page ids separated by commas (bad: {', '.join(sorted(bad))[:80]}).",
            error_class="setup",
        )
    return ids


def allowed_pages(account: str | None = None) -> dict[str, Page]:
    """{page_id: Page} for every allowed Page the token can manage (cached per token)."""
    acct = auth.resolve(account)
    env_ids = allowed_ids_from_env()
    key = (
        acct.label,
        ",".join(sorted(env_ids)) if env_ids is not None else f"ig:{acct.ig_user_id}",
        hashlib.sha256(acct.access_token.encode()).hexdigest(),
    )
    if key in _CACHE:
        return _CACHE[key]
    user = graph_client.GraphClient(
        acct.access_token, base_host=acct.base_host, app_secret=acct.app_secret, timeout=120.0
    )
    rows = user.get("me/accounts", fields="id,name,access_token,tasks,instagram_business_account", limit=100)
    out: dict[str, Page] = {}
    for row in rows.get("data") or []:
        pid = str(row.get("id") or "")
        ig = (row.get("instagram_business_account") or {}).get("id")
        allowed = pid in env_ids if env_ids is not None else (ig is not None and ig == acct.ig_user_id)
        if not allowed or not row.get("access_token"):
            continue  # the token of a forbidden Page is discarded here, never used
        out[pid] = Page(
            id=pid,
            name=row.get("name") or "",
            client=graph_client.GraphClient(
                row["access_token"], base_host=acct.base_host, app_secret=acct.app_secret
            ),
            account=acct.label,
            tasks=tuple(row.get("tasks") or ()),
            ig_user_id=ig,
        )
    if not out:
        where = f"{ALLOWED_ENV}={','.join(sorted(env_ids))}" if env_ids is not None else (
            f"the Page linked to Instagram account {acct.ig_user_id}")
        raise graph_client.GraphAPIError(
            f"No allowed Facebook Page found on this token ({where}).",
            error_class="setup",
            extra={"hint": "Generate the token with the Page selected and pages_show_list granted, "
                           f"and check {ALLOWED_ENV}."},
        )
    _CACHE[key] = out
    return out


def resolve(page_id: str | None = None, account: str | None = None) -> Page:
    """The Page to act on. None -> the only allowed Page (or the first one, sorted by id)."""
    pages = allowed_pages(account)
    if page_id is None or str(page_id).strip() == "":
        return pages[sorted(pages)[0]]
    pid = str(page_id).strip()
    if not _PAGE_ID_RE.match(pid):
        raise V.ValidationError(f"page_id must be a numeric Page id (got {V.truncate(pid, 40)!r})")
    if pid not in pages:
        raise forbidden(pid)
    return pages[pid]


# ----------------------------- object ownership ----------------------------- #

_OBJ_ID_RE = re.compile(r"^\d{1,32}(?:_\d{1,32})?$")


def validate_object_id(value: str, *, field: str) -> str:
    if value is None or not isinstance(value, str):
        raise V.ValidationError(f"{field} must be a string")
    v = value.strip()
    if not _OBJ_ID_RE.match(v):
        raise V.ValidationError(f"{field} must be a Facebook id like 123_456 or 456 (got {V.truncate(v, 40)!r})")
    return v


_FALLTHROUGH = {"not_found", "invalid_param", "permission"}


def _check_owner(page: Page, obj_id: str, *, field: str) -> None:
    """GET id,from and require Graph to echo the SAME id and the Page as author.

    Graph may canonicalise an id to another object; trusting only the id we sent
    would let "<our_page>_<foreign_story>" reach a foreign post.
    """
    try:
        got = page.client.get(obj_id, fields="id,from")
    except graph_client.GraphAPIError as exc:
        if exc.error_class in _FALLTHROUGH:
            raise graph_client.GraphAPIError(
                f"{field} {obj_id} was not found on Page {page.name}.", error_class="not_found"
            ) from exc
        raise
    owner = (got.get("from") or {}).get("id")
    if str(got.get("id")) != obj_id or owner != page.id:
        raise forbidden(str(owner or "unknown"))


def assert_own_post(page: Page, post_id: str, *, field: str = "post_id") -> str:
    """A post/photo/video id that belongs to this Page. Raises forbidden_page otherwise.

    "<page_id>_<n>" ids must carry this Page's id as prefix; every id is then confirmed
    live (same id echoed back, authored by the Page).
    """
    pid = validate_object_id(post_id, field=field)
    if "_" in pid and pid.split("_", 1)[0] != page.id:
        raise forbidden(pid.split("_", 1)[0])
    _check_owner(page, pid, field=field)
    return pid


def assert_own_comment(page: Page, comment_id: str) -> str:
    """A comment id ("<object>_<n>") on an object owned by this Page."""
    cid = validate_object_id(comment_id, field="comment_id")
    if "_" not in cid:
        raise V.ValidationError("comment_id must look like 123_456 (the id from get_comments)")
    obj = cid.split("_", 1)[0]
    # The commented object is either the post "<page>_<obj>" or a photo/video <obj>.
    try:
        _check_owner(page, f"{page.id}_{obj}", field="comment_id")
        return cid
    except graph_client.GraphAPIError as exc:
        if exc.error_class != "not_found":
            raise
    _check_owner(page, obj, field="comment_id")
    return cid
