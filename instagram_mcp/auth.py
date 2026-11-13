"""Multi-account token store for instagram-mcp.

A long-lived Instagram Graph API token is a bearer credential. It is stored in
the macOS Keychain (primary) or a chmod-600 file (fallback on non-macOS / when
the `security` CLI is unavailable), NEVER in the plaintext metadata file and
NEVER returned through a tool output.

Layout:
  ~/.claude/instagram-mcp/accounts.json   metadata ONLY: {label, ig_user_id,
                                           base_host, app_secret_ref, default}.
                                           chmod 600. No tokens.
  Keychain service "instagram-mcp",        token per account label; app secret
  account "<label>" / "<label>::secret"    under the ::secret suffix.

Default-account resolution precedence:
  1. INSTAGRAM_MCP_ACCESS_TOKEN env (+ INSTAGRAM_MCP_IG_USER_ID) -> ephemeral
     single account labelled "env" (zero-config / CI path).
  2. The account flagged default in accounts.json.
  3. The sole account, if exactly one exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from instagram_mcp import graph_client

ACCOUNTS_PATH = Path(
    os.environ.get("INSTAGRAM_MCP_ACCOUNTS_PATH") or os.path.expanduser("~/.claude/instagram-mcp/accounts.json")
)
TOKENS_FALLBACK_PATH = ACCOUNTS_PATH.parent / "tokens.json"  # chmod 600, fallback only
KEYCHAIN_SERVICE = "instagram-mcp"

ENV_TOKEN = "INSTAGRAM_MCP_ACCESS_TOKEN"
ENV_IG_USER_ID = "INSTAGRAM_MCP_IG_USER_ID"
ENV_BASE_HOST = "INSTAGRAM_MCP_BASE_HOST"
ENV_APP_SECRET = "INSTAGRAM_MCP_APP_SECRET"

_DEFAULT_BASE = "graph.facebook.com"


class AuthError(RuntimeError):
    """Raised when an account / token cannot be resolved. Maps to error_class=auth."""

    error_class = "auth"


class UnknownAccountError(AuthError):
    """The caller named an account label that is not configured (an input mistake)."""

    error_class = "validation"


@dataclass
class ResolvedAccount:
    label: str
    ig_user_id: str
    base_host: str
    access_token: str
    app_secret: str | None

    def client(self) -> graph_client.GraphClient:
        return graph_client.GraphClient(self.access_token, base_host=self.base_host, app_secret=self.app_secret)


# --------------------------------------------------------------------------- #
# Keychain (macOS) with chmod-600 file fallback
# --------------------------------------------------------------------------- #


def _keychain_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _keychain_set(account: str, secret: str) -> None:
    """Store via `security -i` reading the command from stdin, so the secret never appears
    in the process list (argv). Failures raise AuthError without echoing the command."""
    if any(ch in secret for ch in '"\\\n\r') or any(ch in account for ch in '"\\\n\r'):
        raise AuthError("token or label contains characters that cannot be stored safely")
    command = f'add-generic-password -U -a "{account}" -s "{KEYCHAIN_SERVICE}" -w "{secret}"\n'
    try:
        res = subprocess.run(["security", "-i"], input=command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise AuthError(f"keychain write failed (security exit {exc.returncode}); is the keychain locked?") from None
    except OSError:
        raise AuthError("keychain write failed (security command unavailable)") from None
    if res.returncode != 0:
        raise AuthError(f"keychain write failed (security exit {res.returncode})")


def _keychain_get(account: str) -> str | None:
    res = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _keychain_delete(account: str) -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE],
        capture_output=True,
        text=True,
    )


def _fallback_load() -> dict[str, str]:
    if not TOKENS_FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_FALLBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_private(path: Path, text: str) -> None:
    """Atomically write a 0600 file (never briefly world-readable) in a 0700 directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _fallback_save(data: dict[str, str]) -> None:
    _write_private(TOKENS_FALLBACK_PATH, json.dumps(data))


def _secret_set(account: str, secret: str) -> None:
    if _keychain_available():
        _keychain_set(account, secret)
        return
    data = _fallback_load()
    data[account] = secret
    _fallback_save(data)


def _secret_get(account: str) -> str | None:
    if _keychain_available():
        return _keychain_get(account)
    return _fallback_load().get(account)


def _secret_delete(account: str) -> None:
    if _keychain_available():
        _keychain_delete(account)
        return
    data = _fallback_load()
    if account in data:
        del data[account]
        _fallback_save(data)


def secret_backend() -> str:
    return "macos-keychain" if _keychain_available() else "chmod600-file"


# --------------------------------------------------------------------------- #
# Metadata file (NO tokens)
# --------------------------------------------------------------------------- #


def _load_meta() -> dict[str, Any]:
    if not ACCOUNTS_PATH.exists():
        return {"accounts": {}, "default": None}
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
        data.setdefault("accounts", {})
        data.setdefault("default", None)
        return data
    except Exception:
        return {"accounts": {}, "default": None}


def _save_meta(meta: dict[str, Any]) -> None:
    _write_private(ACCOUNTS_PATH, json.dumps(meta, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def list_accounts() -> list[dict[str, Any]]:
    """Account metadata only — never tokens.

    Includes the ephemeral env account when INSTAGRAM_MCP_ACCESS_TOKEN is set.
    """
    meta = _load_meta()
    out: list[dict[str, Any]] = []
    if os.environ.get(ENV_TOKEN):
        out.append(
            {
                "label": "env",
                "ig_user_id": os.environ.get(ENV_IG_USER_ID),
                "base_host": os.environ.get(ENV_BASE_HOST, _DEFAULT_BASE),
                "source": "env",
                "default": meta.get("default") in (None, "env"),
            }
        )
    for label, info in meta["accounts"].items():
        out.append(
            {
                "label": label,
                "ig_user_id": info.get("ig_user_id"),
                "base_host": info.get("base_host", _DEFAULT_BASE),
                "source": "stored",
                "has_app_secret": bool(info.get("app_secret")),
                "default": meta.get("default") == label,
            }
        )
    return out


def add_account(
    label: str,
    access_token: str,
    ig_user_id: str,
    *,
    base_host: str = _DEFAULT_BASE,
    app_secret: str | None = None,
    make_default: bool = False,
) -> dict[str, Any]:
    """Store an account's token in the secret backend + metadata in accounts.json."""
    clear_cache()
    if base_host not in graph_client.ALLOWED_HOSTS:
        raise AuthError(f"base_host must be one of {sorted(graph_client.ALLOWED_HOSTS)}")
    if label == "env":
        raise AuthError('"env" is reserved for the INSTAGRAM_MCP_ACCESS_TOKEN account; choose another label')
    _secret_set(label, access_token)
    if app_secret:
        _secret_set(f"{label}::secret", app_secret)
    meta = _load_meta()
    others = [lbl for lbl in meta["accounts"] if lbl != label]
    meta["accounts"][label] = {
        "ig_user_id": ig_user_id,
        "base_host": base_host,
        "app_secret": bool(app_secret),
    }
    # Never silently take over: only the very first account (with no env account active)
    # becomes the default automatically.
    if make_default or (meta.get("default") is None and not others and not os.environ.get(ENV_TOKEN)):
        meta["default"] = label
    _save_meta(meta)
    return {
        "label": label,
        "ig_user_id": ig_user_id,
        "base_host": base_host,
        "default": meta["default"] == label,
        "secret_backend": secret_backend(),
    }


def remove_account(label: str) -> dict[str, Any]:
    clear_cache()
    meta = _load_meta()
    existed = label in meta["accounts"]
    _secret_delete(label)
    _secret_delete(f"{label}::secret")
    if existed:
        del meta["accounts"][label]
    if meta.get("default") == label:
        # keep a default only when it is unambiguous (exactly one account left)
        remaining = list(meta["accounts"])
        meta["default"] = remaining[0] if len(remaining) == 1 else None
    _save_meta(meta)
    return {"label": label, "removed": existed, "default": meta.get("default")}


def set_default(label: str) -> dict[str, Any]:
    clear_cache()
    meta = _load_meta()
    if label not in meta["accounts"] and not (label == "env" and os.environ.get(ENV_TOKEN)):
        raise AuthError(f"unknown account {label!r}")
    meta["default"] = label
    _save_meta(meta)
    return {"default": label}


# Resolved accounts cached in memory: avoids 1-2 keychain subprocess calls per tool call.
_RESOLVE_CACHE: dict[tuple[Any, ...], ResolvedAccount] = {}
_RESOLVE_LOCK = threading.Lock()


def clear_cache() -> None:
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE.clear()


def _cache_key(label: str | None) -> tuple[Any, ...]:
    try:
        mtime = ACCOUNTS_PATH.stat().st_mtime_ns
    except OSError:
        mtime = None
    env = os.environ.get(ENV_TOKEN) or ""
    return (label, mtime, hashlib.sha256(env.encode()).hexdigest(), os.environ.get(ENV_IG_USER_ID))


def resolve(label: str | None = None) -> ResolvedAccount:
    """Resolve label -> ResolvedAccount (cached until accounts.json or the env account changes)."""
    key = _cache_key(label)
    with _RESOLVE_LOCK:
        hit = _RESOLVE_CACHE.get(key)
    if hit is not None:
        return hit
    acct = _resolve_uncached(label)
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE[key] = acct
    return acct


def _resolve_uncached(label: str | None = None) -> ResolvedAccount:
    """Resolve label -> ResolvedAccount (with token). Internal use only."""
    meta = _load_meta()
    # Explicit env account, or default falling through to env.
    if label == "env" or (label is None and os.environ.get(ENV_TOKEN) and meta.get("default") in (None, "env")):
        token = os.environ.get(ENV_TOKEN)
        if not token:
            raise AuthError(f"{ENV_TOKEN} not set")
        uid = os.environ.get(ENV_IG_USER_ID)
        if uid and not uid.strip().isdigit():
            raise AuthError(f"{ENV_IG_USER_ID} must be the numeric Instagram account id")
        if not uid:
            raise AuthError(f"{ENV_IG_USER_ID} must be set alongside {ENV_TOKEN}")
        return ResolvedAccount(
            "env", uid, os.environ.get(ENV_BASE_HOST, _DEFAULT_BASE), token, os.environ.get(ENV_APP_SECRET)
        )

    target = label or meta.get("default")
    if not target:
        if os.environ.get(ENV_TOKEN):
            return resolve("env")
        if meta["accounts"]:
            raise AuthError(
                "several accounts are configured but none is the default; pass account=<label> "
                "or call set_default_account"
            )
        raise AuthError("no account configured — set INSTAGRAM_MCP_ACCESS_TOKEN or call add_account")
    info = meta["accounts"].get(target)
    if not info:
        raise UnknownAccountError(f"unknown account {target!r} — call list_accounts to see configured accounts")
    token = _secret_get(target)
    if not token:
        raise AuthError(f"no stored token for account {target!r} ({secret_backend()}) — re-run add_account")
    app_secret = _secret_get(f"{target}::secret") if info.get("app_secret") else None
    return ResolvedAccount(target, info["ig_user_id"], info.get("base_host", _DEFAULT_BASE), token, app_secret)


def client_for(label: str | None = None) -> tuple[graph_client.GraphClient, ResolvedAccount]:
    acct = resolve(label)
    return acct.client(), acct
