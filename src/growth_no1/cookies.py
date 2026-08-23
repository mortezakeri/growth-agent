"""Cookie parsing: Cookie Editor V3 exports -> Playwright-ready cookie lists.

Supported input formats:
1. Array of cookie objects:
   [{"name": "auth_token", "value": "...", "domain": ".x.com", ...}, ...]
2. Simple object:
   {"auth_token": "...", "ct0": "..."}

Validation requires auth_token and ct0. Errors never include cookie values.
sameSite is normalized to Playwright's Strict | Lax | None.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

REQUIRED = ("auth_token", "ct0")
VALID_SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None",
                  "no_restriction": "None", "unspecified": "Lax"}


class CookieError(ValueError):
    pass


def _normalize_same_site(raw) -> str | None:
    if raw is None:
        return None
    norm = VALID_SAMESITE.get(str(raw).strip().lower())
    if norm is None:
        raise CookieError(f"unsupported sameSite value: {raw!r}")
    return norm


def _from_editor_object(c: dict) -> dict:
    if not isinstance(c, dict) or "name" not in c or "value" not in c:
        raise CookieError("cookie entry missing 'name'/'value'")
    out = {
        "name": str(c["name"]),
        "value": str(c["value"]),
        "domain": c.get("domain") or ".x.com",
        "path": c.get("path") or "/",
    }
    if c.get("expirationDate") is not None:
        out["expires"] = int(float(c["expirationDate"]))
    elif c.get("expires") is not None:
        out["expires"] = int(float(c["expires"]))
    if "httpOnly" in c:
        out["httpOnly"] = bool(c["httpOnly"])
    if "secure" in c:
        out["secure"] = bool(c["secure"])
    ss = _normalize_same_site(c.get("sameSite"))
    if ss:
        out["sameSite"] = ss
    return out


def parse_cookies(raw: str | bytes | list | dict) -> list[dict]:
    """Parse any supported format into a Playwright-compatible cookie list."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CookieError(f"X_COOKIES_JSON is not valid JSON: {e.msg} "
                              f"(char {e.pos})") from None

    if isinstance(raw, dict):
        if all(isinstance(v, (str, int)) for v in raw.values()):
            # simple {name: value} map
            cookies = [{"name": k, "value": str(v), "domain": ".x.com", "path": "/"}
                       for k, v in raw.items()]
            entries = [c["name"] for c in cookies]
        else:
            # single editor-style object wrapping a list? tolerate {"cookies": [...]}
            inner = raw.get("cookies")
            if isinstance(inner, list):
                return parse_cookies(inner)
            raise CookieError("cookie object values must be strings")
    elif isinstance(raw, list):
        cookies = [_from_editor_object(c) for c in raw]
    else:
        raise CookieError(f"unsupported cookie payload type: {type(raw).__name__}")

    names = [c["name"] for c in cookies]
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        raise CookieError(
            f"missing required cookie(s): {', '.join(missing)} — "
            f"got: {', '.join(sorted(set(names)))}")
    return cookies


def as_simple_dict(cookies: list[dict]) -> dict[str, str]:
    """{name: value} view for legacy call sites."""
    return {c["name"]: c["value"] for c in cookies}


def load_cookie_source(env_var: str = "X_COOKIES_JSON") -> list[dict]:
    """Env var first, then gitignored data/cookies.json fallback."""
    raw = os.environ.get(env_var)
    if raw:
        return parse_cookies(raw)
    p = ROOT / "data" / "cookies.json"
    if not p.exists():
        raise CookieError(
            f"no cookies found: set {env_var} or create {p} "
            "(Cookie Editor V3 export or {'auth_token': ..., 'ct0': ...})")
    return parse_cookies(p.read_text(encoding="utf-8"))
