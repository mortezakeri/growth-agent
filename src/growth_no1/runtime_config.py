"""Live runtime configuration store.

Single source of truth: config/settings.json. Every Telegram mutation goes
through save() so changes survive restarts. Readers (runner, nous_client)
call get() each cycle so edits apply without downtime.
"""
from __future__ import annotations

import json
import threading
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
        "max_words": 15,
        "skill_prompt": None,   # injected behavioral instructions
        "style_override": None, # witty | analytical | supportive | custom
    },
    "provider": {
        "name": "nous",
        "endpoint": "https://inference-api.nousresearch.com/v1/chat/completions",
        "model": "ox-alpha",
        "api_key_env": "NOUS_API_KEY",
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
    return _deep_merge(_DEFAULTS, data)


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
        "model": "gemini-3.6-flash",
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
