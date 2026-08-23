"""Nous Portal API client (OpenAI-compatible) for text + multimodal calls.

Auth via NOUS_API_KEY environment variable. Model: 0x-alpha.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
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


def _post(payload: dict, timeout: int = 120, retries: int = 2) -> dict:
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
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                raise
            time.sleep(3 * (2 ** attempt))
    raise RuntimeError("provider request failed")


def chat(prompt: str, system: str = "You are a concise social media assistant.") -> str:
    """Plain text completion."""
    resp = _post({
        "model": NOUS_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 300,
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


DRAFT_SYSTEM = """You are an elite Crypto Twitter / Web3 reply agent.

Read the ORIGINAL tweet supplied by the user and write ONE short, natural reply
to that ORIGINAL tweet. Never reply to replies, comments, quoted tweets, or
nested conversations.

Hard rules:
- Maximum 15 words. Count words before answering.
- Use lowercase by default.
- Output only the reply text: no explanation, analysis, alternatives, prefix,
  quotation marks, or hashtags.
- Never sound like an AI, bot, marketer, or engagement farmer.
- If the tweet does not deserve a natural reply, output exactly: SKIP

Voice: casual, sharp, witty, slightly playful, internet-native, confident but
not try-hard, sometimes understated, and natural enough to look manually typed.
Avoid generic praise, corporate language, motivational cliches, fake enthusiasm,
repetitive structures, obvious engagement bait, excessive emojis, and phrases
such as "great insight", "well said", "this is huge", or "couldn't agree more".

First classify the tweet internally as: morning/greeting, crypto/Web3 opinion,
AI/agents/technology, product announcement, news/market update, meme/joke,
personal building progress, question, educational/technical, or other. Do not
output the classification. For technical, opinion, news, product, AI, and
educational tweets, prioritize a contextual response over a greeting. For an
obvious morning/weekend tweet, a creative CT-native greeting is appropriate.

Every reply must begin with the REQUIRED OPENING supplied by the user. The
openings rotate in this exact order: good morning; top of the morning; gmorning;
gee eem; g to your em; rise and shine; grand rising; awesome morning. Keep the
first two especially warm and natural, in the spirit of "good morning legend,
wish you an awesome day ahead" and "top of the morning champ, hope your day
started great". These examples define the style, not fixed text to copy.

When adding a wish or a phrase containing hope, place a natural CT address such
as legend, champ, fren, friend, degen, ser, anon, or builder immediately before
the wish/hope section. Rotate these addresses and never force one when it makes
the sentence awkward. Connect the rest of the reply to the original tweet.

Look for wordplay in the original tweet. Match jokes instead of forcing a
serious response. Before answering, silently verify that the reply is human,
relevant to the original tweet, adds something small, is at most 16 words,
does not repeat a recent structure supplied by the user, is at most 15 words, and does not feel
forced. Rewrite if needed; otherwise output SKIP."""

GREETING_OPENINGS = (
    "good morning",
    "top of the morning",
    "gmorning",
    "gee eem",
    "g to your em",
    "rise and shine",
    "grand rising",
    "awesome morning",
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


def _clean_reply(text: str, max_words: int = 15) -> str:
    """Normalize output; enforce no emoji and the word cap in code."""
    cleaned = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u200d\ufe0f]",
        "", text,
    )
    cleaned = " ".join(cleaned.strip().strip('"').split())
    return " ".join(cleaned.split()[:max_words]).strip()


def _next_greeting_opening(recent_replies: list[str] | None) -> str:
    """Continue the configured opening rotation; a fresh history starts first."""
    recent = [r.strip().lower() for r in (recent_replies or []) if r.strip()]
    for reply in reversed(recent):
        for index, opening in enumerate(GREETING_OPENINGS):
            if reply == opening or reply.startswith(opening + " ") or reply.startswith(opening + ","):
                return GREETING_OPENINGS[(index + 1) % len(GREETING_OPENINGS)]
    return GREETING_OPENINGS[0]


def generate_drafts_with_sources(author: str, tweet_text: str,
                                 styles: tuple[str, ...],
                                 recent_replies: list[str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Return draft bodies plus an accurate llm/local_fallback source per style."""
    from drafts import _TEMPLATES  # local templates as fallback
    import random
    rng = random.Random()

    recent = "\n".join(f"- {r}" for r in (recent_replies or [])[-12:])
    required_opening = _next_greeting_opening(recent_replies)
    prompt = (
        f"ORIGINAL TWEET by @{author}:\n{tweet_text}\n\n"
        + (f"RECENT REPLIES — avoid their openings and structures:\n{recent}\n\n" if recent else "")
        + f"REQUIRED OPENING: {required_opening}\n"
        + "Write one contextual reply beginning exactly with that opening. "
          "Maximum 15 words. Return only the reply, or SKIP."
    )
    out: dict[str, str] = {}
    sources: dict[str, str] = {}
    try:
        raw = chat(prompt, system=_system_prompt()).strip()
        # Accept the old single labeled form during rollout, but the new prompt
        # asks the provider for one unlabelled answer.
        if len(raw.splitlines()) == 1 and ":" in raw:
            maybe_style, _, maybe_body = raw.partition(":")
            if maybe_style.strip().lower().replace("-", "_") in styles:
                raw = maybe_body
        cleaned = _clean_reply(raw, max_words=15)
        if cleaned.upper() == "SKIP":
            for style in styles:
                out[style], sources[style] = "SKIP", "llm_skip"
        elif cleaned:
            for style in styles:
                out[style], sources[style] = cleaned, "llm"
    except Exception as e:
        print(f"[nous] draft generation failed ({e}); using templates")
    for s in styles:
        if s not in out:
            out[s] = rng.choice(_TEMPLATES[s]).format(author=author or "friend", topic="web3").lower()
            sources[s] = "local_fallback"
    return out, sources


def generate_drafts(author: str, tweet_text: str, styles: tuple[str, ...]) -> dict[str, str]:
    """Backward-compatible bodies-only wrapper."""
    return generate_drafts_with_sources(author, tweet_text, styles)[0]
