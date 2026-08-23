"""Web3 / GM tweet scout engine.

X search via Playwright using cookie auth (auth_token + ct0). X uses POST for
many read-only GraphQL operations, so known mutation operations are blocked
instead of blocking every POST indiscriminately.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

MUTATION_MARKERS = (
    "CreateTweet", "DeleteTweet", "FavoriteTweet", "UnfavoriteTweet",
    "CreateRetweet", "DeleteRetweet", "Follow", "Unfollow",
)


def _is_x_mutation(request) -> bool:
    return request.method in {"PUT", "PATCH", "DELETE"} or (
        request.method == "POST" and any(marker in request.url for marker in MUTATION_MARKERS)
    )


@dataclass
class TweetCandidate:
    id: str
    author: str
    text: str
    url: str
    has_image: bool = False
    image_urls: list[str] | None = None
    score: float = 0.0


class ScoutEngine:
    """Cookie-authenticated, read-only tweet scout.

    Usage:
        engine = ScoutEngine(cookies={"auth_token": ..., "ct0": ...})
        tweets = engine.search("gm web3", limit=20)
    """

    SEARCH_URL = "https://x.com/search?q={query}&f=live"

    def __init__(self, cookies: dict[str, str], keywords: list[str],
                 min_interval_s: int = 240, max_interval_s: int = 720,
                 headless: bool = True):
        if "auth_token" not in cookies or "ct0" not in cookies:
            raise ValueError("both auth_token and ct0 cookies required")
        self.cookies = cookies
        self.keywords = [k.lower() for k in keywords]
        self.interval = (min_interval_s, max_interval_s)
        self.headless = headless
        self._rng = random.Random()
        self._last_request_ts = 0.0

    def _polite_delay(self) -> None:
        wait = self._rng.uniform(*self.interval)
        elapsed = time.time() - self._last_request_ts
        if elapsed < wait:
            time.sleep(wait - elapsed)

    def search(self, query: str, limit: int = 20) -> list[TweetCandidate]:
        from playwright.sync_api import sync_playwright

        self._polite_delay()
        self._last_request_ts = time.time()
        candidates: list[TweetCandidate] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
            )
            # Keep X's read-only POST GraphQL calls working; block known writes.
            ctx.route("**/*", lambda route: (
                route.abort() if _is_x_mutation(route.request) else route.continue_()
            ))
            base_url = "https://x.com"
            ctx.add_cookies([
                {"name": n, "value": v, "domain": ".x.com", "path": "/",
                 "httpOnly": n == "auth_token", "secure": True}
                for n, v in self.cookies.items()
            ])
            page = ctx.new_page()
            page.goto(self.SEARCH_URL.format(query=query), wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            articles = page.query_selector_all("article[data-testid='tweet']")
            seen: set[str] = set()
            for art in articles[:limit]:
                try:
                    text_el = art.query_selector("div[data-testid='tweetText']")
                    text = text_el.inner_text() if text_el else ""
                    link = art.query_selector("a[href*='/status/']")
                    href = link.get_attribute("href") if link else ""
                    author = ""
                    m = re.match(r"/([^/]+)/status/", href or "")
                    if m:
                        author = m.group(1)
                    imgs = art.query_selector_all("img[src*='pbs.twimg.com/media']")
                    tid = (href or "").rstrip("/").rsplit("/", 1)[-1]
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    candidates.append(TweetCandidate(
                        id=tid, author=author, text=text.strip(),
                        url=f"{base_url}/{author}/status/{tid}",
                        has_image=bool(imgs),
                        image_urls=[i.get_attribute("src") for i in imgs] or [],
                    ))
                except Exception:
                    continue
            browser.close()
        return candidates

    def keyword_score(self, c: TweetCandidate) -> float:
        text = c.text.lower()
        score = sum(2.0 for k in self.keywords if k in text)
        score += 1.5 if re.fullmatch(r"(gm|gn)\W*", text) else 0.0
        score += 1.0 if c.has_image else 0.0
        c.score = score
        return score


def save_candidates(candidates: list[TweetCandidate], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    known_ids = {e["id"] for e in existing}
    new = [asdict(c) for c in candidates if c.id not in known_ids]
    path.write_text(json.dumps(existing + new, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
