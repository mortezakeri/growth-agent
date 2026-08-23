"""Live runtime configuration store.

Single source of truth: config/settings.json. Every Telegram mutation goes
through save() so changes survive restarts. Readers (runner, nous_client)
call get() each cycle so edits apply without downtime.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.json"

_LOCK = threading.Lock()

_DEFAULTS = {
    "timezone": "Asia/Tehran",
    "working_windows": [
        {"name": "morning", "start": "06:00", "end": "12:00", "max_replies": 5},
        {"name": "evening", "start": "12:30", "end": "01:00", "crosses_midnight": True,
         "max_replies": 5},
    ],
    "read_interval_seconds": {"min": 240, "max": 720},
    "scout": {
        "keywords": ["gm", "web3", "crypto", "bitcoin", "eth", "solana", "ai agents"],
        "max_tweets_per_session": 25,
    },
    "drafts": {
        "styles": ["gm", "observant", "curious", "dry_humor"],
        "max_words": 16,
        "skill_prompt": None,   # injected behavioral instructions
        "style_override": None, # witty | analytical | supportive | custom
    },
    "provider": {
        "name": "gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.5-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
    },
    "telegram": {"bot_token": None, "chat_id": None},
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with _LOCK:
        if SETTINGS_PATH.exists():
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        else:
            data = {}
    # Overlay safe cloud runtime state (data/cloud_runtime.json) on top.
    return _deep_merge(_deep_merge(_DEFAULTS, data), _load_cloud_overlay())


CLOUD_RUNTIME_PATH = ROOT / "data" / "cloud_runtime.json"

# Only these keys may live in the cloud overlay — never cookies/tokens/keys.
# metrics removed entirely: unrestricted nested dict, unused in runtime flow,
# and too easy to smuggle secret-like strings under arbitrary metric names.
_CLOUD_SAFE_KEYS = {"update_offset", "paused", "working_windows", "drafts",
                    "last_run", "last_successful_run", "last_scout_count",
                    "last_error_summary", "updated_at"}


def _safe_drafts(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    prompt = value.get("skill_prompt")
    style = value.get("style_override")
    if prompt is None or isinstance(prompt, str):
        out["skill_prompt"] = prompt[:500] if isinstance(prompt, str) else None
    if style is None or style in ("witty", "analytical", "supportive", "custom"):
        out["style_override"] = style
    return out


def _safe_time(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except (ValueError, TypeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _safe_windows(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for window in value:
        if not isinstance(window, dict):
            continue
        name = window.get("name")
        start, end = _safe_time(window.get("start")), _safe_time(window.get("end"))
        cap = window.get("max_replies")
        if (name not in ("morning", "evening") or not start or not end or
                isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= 100):
            continue
        out.append({"name": name, "start": start, "end": end,
                    "crosses_midnight": end < start, "max_replies": cap})
    return out


def _sanitize_cloud_state(raw) -> dict:
    """Return a fully allow-listed, JSON-safe cloud state."""
    if not isinstance(raw, dict):
        return {}
    out = {k: v for k, v in raw.items()
           if k in ("last_run", "last_successful_run", "updated_at")
           and isinstance(v, str)}
    offset = raw.get("update_offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        out["update_offset"] = offset
    if isinstance(raw.get("paused"), bool):
        out["paused"] = raw["paused"]
    count = raw.get("last_scout_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        out["last_scout_count"] = count
    error = raw.get("last_error_summary")
    if isinstance(error, str):
        out["last_error_summary"] = error[:300]
    if "drafts" in raw:
        out["drafts"] = _safe_drafts(raw["drafts"])
    if "working_windows" in raw:
        out["working_windows"] = _safe_windows(raw["working_windows"])
    return out


def _load_cloud_overlay() -> dict:
    try:
        raw = json.loads(CLOUD_RUNTIME_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    clean = _sanitize_cloud_state(raw)
    overlay = {}
    if isinstance(clean.get("working_windows"), list):
        overlay["working_windows"] = clean["working_windows"]
    if isinstance(clean.get("drafts"), dict):
        overlay["drafts"] = {k: v for k, v in clean["drafts"].items()
                             if k in ("skill_prompt", "style_override")}
    return overlay


def load_cloud_state() -> dict:
    """Raw cloud state (offset, paused, counters) for telegram_once/runner.

    Hardened: invalid update_offset does not crash (defaults to 0); paused
    only accepts a real Python bool (so "false" does not become True); malformed
    state types fall back safely without echoing bad values.
    """
    try:
        state = json.loads(CLOUD_RUNTIME_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    # update_offset: must be a non-negative int; everything else silently -> 0.
    raw_offset = state.get("update_offset")
    if isinstance(raw_offset, bool):
        # JSON booleans are a distinct type from int in Python — treat as 0.
        update_offset = 0
    elif isinstance(raw_offset, int) and not isinstance(raw_offset, bool):
        update_offset = max(0, raw_offset)
    else:
        update_offset = 0

    # paused: only a real Python bool counts; truthy strings like "false"
    # must NOT flip the state.
    paused = False
    raw_paused = state.get("paused")
    if isinstance(raw_paused, bool):
        paused = raw_paused

    def _safe_text(v) -> str:
        if not isinstance(v, str):
            return ""
        return v[:300]

    def _safe_ts(v) -> str | None:
        if isinstance(v, str) and v:
            return v
        return None

    return {
        "update_offset": update_offset,
        "paused": paused,
        "last_run": _safe_ts(state.get("last_run")),
        "last_successful_run": _safe_ts(state.get("last_successful_run")),
        "last_scout_count": int(state["last_scout_count"])
        if isinstance(state.get("last_scout_count"), (int, float)) else None,
        "last_error_summary": _safe_text(state.get("last_error_summary")),
        "updated_at": _safe_ts(state.get("updated_at")),
    }


def save_cloud_state(**updates) -> dict:
    """Persist safe values only.

    Security: the ENTIRE existing state file is sanitized against the top-level
    allow-list before the new updates are merged. This closes a TOCTOU class
    where unknown fields injected into cloud_runtime.json (cookies, auth tokens,
    API keys, authorization headers, arbitrary unknown keys) would otherwise
    survive across saves — they are removed on the next write.
    """
    with _LOCK:
        CLOUD_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = json.loads(CLOUD_RUNTIME_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}

        # 1. Sanitize the entire existing state, including nested structures.
        state = _sanitize_cloud_state(state)

        # 2. Drop keys from the incoming patch that are not in the allow-list.
        safe = {k: v for k, v in updates.items() if k in _CLOUD_SAFE_KEYS}

        # 3. Deep-sanitize nested structures the allow-list permits.
        if "drafts" in safe:
            safe["drafts"] = _safe_drafts(safe["drafts"])
        if "working_windows" in safe:
            safe["working_windows"] = _safe_windows(safe["working_windows"])
        if "last_error_summary" in safe:
            safe["last_error_summary"] = str(safe["last_error_summary"])[:300]

        state.update(safe)
        state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = CLOUD_RUNTIME_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CLOUD_RUNTIME_PATH)
        return state


def save(mutator) -> dict:
    """mutator(cfg_dict) mutates in place; result persisted atomically."""
    with _LOCK:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) \
            if SETTINGS_PATH.exists() else {}
        cfg = _deep_merge(_DEFAULTS, cfg)
        mutator(cfg)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        return cfg


# ------------------------------------------------------------- mutations

def set_window_limit(window_name: str, max_replies: int) -> dict:
    if not 0 <= int(max_replies) <= 100:
        raise ValueError("max replies must be between 0 and 100")
    def m(cfg):
        for w in cfg["working_windows"]:
            if w["name"] == window_name:
                w["max_replies"] = max(0, int(max_replies))
                return
        raise KeyError(f"unknown window '{window_name}'")
    return save(m)


def set_window_hours(window_name: str, start: str, end: str) -> dict:
    def _parse(s):
        h, mi = s.split(":")
        h, mi = int(h), int(mi)
        if not 0 <= h <= 23 or not 0 <= mi <= 59:
            raise ValueError("time must be valid HH:MM")
        return f"{h:02d}:{mi:02d}"
    start, end = _parse(start), _parse(end)

    def m(cfg):
        for w in cfg["working_windows"]:
            if w["name"] == window_name:
                w["start"], w["end"] = start, end
                w["crosses_midnight"] = end < start
                return
        raise KeyError(f"unknown window '{window_name}'")
    return save(m)


def set_skill(prompt_text: str | None, style_override: str | None = None) -> dict:
    def m(cfg):
        d = cfg["drafts"]
        if prompt_text is not None:
            d["skill_prompt"] = prompt_text.strip() or None
        if style_override is not None:
            d["style_override"] = style_override.strip().lower() or None
    return save(m)


PROVIDER_DEFAULTS = {
    "nous": {
        "endpoint": "https://inference-api.nousresearch.com/v1/chat/completions",
        "model": "ox-alpha",
        "api_key_env": "NOUS_API_KEY",
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "model": None,
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "gemini": {
        # OpenAI-compatible Gemini endpoint; key via GEMINI_API_KEY
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-3.5-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
    },
}

# Gitignored store for runtime API keys set via Telegram. Never committed.
SECRETS_PATH = ROOT / "config" / "secrets.json"


def _read_secrets() -> dict:
    if SECRETS_PATH.exists():
        try:
            return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _write_secrets(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(SECRETS_PATH)


def mask_key(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "(set)"
    return f"{key[:4]}…{key[-4:]}"


def set_provider(name: str, api_key: str | None = None,
                 endpoint: str | None = None, model: str | None = None) -> dict:
    """Persist public provider identity to settings.json; the KEY goes only to
    the gitignored config/secrets.json."""
    def m(cfg):
        p = cfg["provider"]
        provider = name.strip().lower()
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        p["name"] = provider
        p["api_key_env"] = defaults.get("api_key_env", f"{provider.upper()}_API_KEY")
        if endpoint:
            p["endpoint"] = endpoint.strip()
        elif defaults.get("endpoint"):
            p["endpoint"] = defaults["endpoint"]
        if model:
            p["model"] = model.strip()
        elif defaults.get("model"):
            p["model"] = defaults["model"]
    cfg = save(m)
    if api_key:
        secrets = _read_secrets()
        secrets[name.strip().lower()] = api_key.strip()
        _write_secrets(secrets)
    return cfg


def get_active_api_key() -> tuple[str | None, str]:
    """(key, source) — secrets.json first, then env var of the active provider."""
    import os
    p = load()["provider"]
    key = _read_secrets().get(p["name"])
    if key:
        return key, "secrets.json"
    key = os.environ.get(p["api_key_env"], "")
    return (key, "env") if key else (None, "none")


def masked_summary() -> str:
    cfg = load()
    p = cfg["provider"]
    key, source = get_active_api_key()
    d = cfg["drafts"]
    return (f"provider: {p['name']}\nendpoint: {p['endpoint']}\nmodel: {p['model']}\n"
            f"key ({p['api_key_env']}, {source}): {mask_key(key)}\n"
            f"style: {d.get('style_override') or '(default styles)'}\n"
            f"skill_prompt: {(d.get('skill_prompt') or '(none)')[:120]}")
