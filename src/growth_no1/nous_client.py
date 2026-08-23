"""Nous Portal API client (OpenAI-compatible) for text + multimodal calls.

Auth via NOUS_API_KEY environment variable. Model: 0x-alpha.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request

NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_MODEL = "ox-alpha"


def _active_provider() -> dict:
    """Reads live runtime config each call — Telegram edits apply instantly."""
    try:
        import runtime_config
        return runtime_config.load()["provider"]
    except Exception:
        return {"name": "nous", "endpoint": NOUS_BASE_URL, "model": NOUS_MODEL,
                "api_key_env": "NOUS_API_KEY"}


def _resolve_key_and_endpoint() -> tuple[str, str, str]:
    """Returns (key, endpoint, model) from secrets.json / env, or raises."""
    prov = _active_provider()
    try:
        import runtime_config
        key, _src = runtime_config.get_active_api_key()
        if key:
            return key, prov.get("endpoint", NOUS_BASE_URL), prov.get("model", NOUS_MODEL)
    except Exception:
        key = prov.get("api_key") or os.environ.get(prov.get("api_key_env") or "", "")
        if key:
            return key, prov.get("endpoint", NOUS_BASE_URL), prov.get("model", NOUS_MODEL)
    raise RuntimeError(
        f"no API key for provider '{prov['name']}' "
        f"(env {prov.get('api_key_env')} or /set_api)")


def _key() -> str:
    return _resolve_key_and_endpoint()[0]


def _post(payload: dict, timeout: int = 120) -> dict:
    key, endpoint, model = _resolve_key_and_endpoint()
    payload = dict(payload, model=model)
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def chat(prompt: str, system: str = "You are a concise social media assistant.") -> str:
    """Plain text completion."""
    resp = _post({
        "model": NOUS_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 300,
        "temperature": 0.9,
    })
    return resp["choices"][0]["message"]["content"].strip()


def vision_prompt(prompt: str, image_bytes: bytes,
                  media_type: str = "image/png") -> str:
    """Multimodal completion: prompt + base64 image."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = _post({
        "model": NOUS_MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{b64}"}},
            ]},
        ],
        "max_tokens": 400,
    })
    return resp["choices"][0]["message"]["content"].strip()


# Hooks matching growth_no1.vision's llm_call signature
def vision_llm_call(prompt: str, b64_png: str) -> str:
    image = base64.b64decode(b64_png)
    return vision_prompt(prompt, image)


DRAFT_SYSTEM = (
    "Write reply drafts for X (Twitter). Rules: all lowercase, no emoji, "
    "no punctuation at end, max 15 words, English only. Web3-native tone."
)

STYLE_INSTRUCTIONS = {
    "witty": "Be witty and playful; light wordplay is welcome.",
    "analytical": "Be analytical; reference concrete mechanics or data.",
    "supportive": "Be supportive and encouraging; genuine community energy.",
}


def _system_prompt() -> str:
    """Base rules + live skill/style injection from runtime config."""
    try:
        import runtime_config
        d = runtime_config.load()["drafts"]
    except Exception:
        d = {}
    parts = [DRAFT_SYSTEM]
    style = d.get("style_override")
    if style and style in STYLE_INSTRUCTIONS:
        parts.append(STYLE_INSTRUCTIONS[style])
    if d.get("skill_prompt"):
        parts.append(f"Additional behavioral instructions: {d['skill_prompt']}")
    return "\n".join(parts)


def generate_drafts(author: str, tweet_text: str, styles: tuple[str, ...]) -> dict[str, str]:
    """LLM-powered drafts; returns {style: body}. Falls back per-style on errors."""
    from drafts import _TEMPLATES  # local templates as fallback
    import random
    rng = random.Random()

    prompt = (
        f"Tweet by @{author}: \"{tweet_text}\"\n\n"
        f"Write {len(styles)} short replies, one per line, labeled:\n"
        + "\n".join(f"{s}: <reply>" for s in styles)
        + "\nEach reply must follow the system rules."
    )
    out: dict[str, str] = {}
    try:
        raw = chat(prompt, system=_system_prompt())
        for line in raw.splitlines():
            if ":" not in line:
                continue
            style, _, body = line.partition(":")
            style = style.strip().lower().replace(" ", "_").replace("-", "_")
            if style in styles and body.strip():
                out[style] = body.strip()[:120]
    except Exception as e:
        print(f"[nous] draft generation failed ({e}); using templates")
    for s in styles:
        if s not in out:
            out[s] = rng.choice(_TEMPLATES[s]).format(author=author or "friend", topic="web3").lower()
    return out
