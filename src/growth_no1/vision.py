"""Multimodal image analysis engine for scout candidates.

Sends tweet images to the active Hermes inference provider via ctx.llm-style
multimodal prompts; falls back to a local heuristic description when no
provider is wired. Read-only: never touches X.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

try:  # optional urllib fetch for remote images
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    urlopen = None


@dataclass
class ImageAnalysis:
    tweet_id: str
    description: str
    is_web3_related: bool
    is_gm_post: bool
    meme: bool
    chart: bool


GM_MARKERS = ("gm", "good morning", "sunrise", "coffee")


def analyze_image_bytes(tweet_id: str, image_bytes: bytes,
                        llm_call=None) -> ImageAnalysis:
    """llm_call(prompt: str, b64_png: str) -> str  — provider hook.

    Defaults to the Nous Portal client (0x-alpha) when NOUS_API_KEY is set;
    falls back to local heuristics otherwise.
    """
    if llm_call is None:
        import os
        if os.environ.get("NOUS_API_KEY"):
            from nous_client import vision_llm_call
            llm_call = vision_llm_call
        else:
            return _heuristic(tweet_id, "", image_bytes)
    prompt = (
        "Describe this tweet image in one sentence. Then answer strictly as JSON: "
        '{"is_web3_related": bool, "is_gm_post": bool, "meme": bool, "chart": bool}.'
    )
    raw = llm_call(prompt, base64.b64encode(image_bytes).decode())
    try:
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        data = {}
    return ImageAnalysis(
        tweet_id=tweet_id,
        description=data.get("description", ""),
        is_web3_related=bool(data.get("is_web3_related")),
        is_gm_post=bool(data.get("is_gm_post")),
        meme=bool(data.get("meme")),
        chart=bool(data.get("chart")),
    )


def analyze_image_url(tweet_id: str, url: str, llm_call=None) -> ImageAnalysis:
    if urlopen is None:
        raise RuntimeError("urllib unavailable")
    with urlopen(url, timeout=30) as r:  # noqa: S310 - fixed media host
        return analyze_image_bytes(tweet_id, r.read(), llm_call)


def _heuristic(tweet_id: str, description: str, image_bytes: bytes) -> ImageAnalysis:
    size = len(image_bytes)
    return ImageAnalysis(
        tweet_id=tweet_id,
        description=description or f"local placeholder ({size} bytes)",
        is_web3_related=False,
        is_gm_post=False,
        meme=False,
        chart=False,
    )


def save_analyses(rows: list[ImageAnalysis], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.__dict__ for r in rows], indent=2), encoding="utf-8")
    return path
