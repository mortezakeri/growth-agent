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


# --------------------------------------------------------------- safety gate

DEFAULT_BLOCKED_TERMS = [
    # politics / elections
    "election", "vote", "voting", "ballot", "senate", "congress", "parliament",
    "president", "prime minister", "democrat", "republican", "leftist",
    "right-wing", "liberal", "conservative", "campaign", "political",
    "politics", "union", "unions", "strike", "protest", "riot",
    # violence / war / hate
    "war", "genocide", "terror", "terrorist", "attack", "killed", "killing",
    "shooting", "hostage", "military", "missile", "drone strike", "hate",
    "racist", "nazi", "slur", "violence", "violent", "weapon", "gun",
]


def evaluate_candidate(
    text: str,
    keywords: list[str],
    min_matches: int = 2,
    blocked_terms: list[str] | None = None,
) -> dict:
    """Strict relevance + safety gate. Blocked term overrides everything;
    image presence or X-search placement never count toward eligibility."""
    lower = text.lower()

    def contains_term(term: str) -> bool:
        # Whole tokens/phrases only: "eth" must not match "together" and
        # "union" must not match "reunion".
        pieces = [re.escape(piece) for piece in term.lower().split()]
        pattern = r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)"
        return bool(re.search(pattern, lower))

    matched = [k for k in keywords if k and contains_term(k)]
    blocked = [t for t in (blocked_terms or []) if t and contains_term(t)]
    if blocked:
        return {"approved": False, "matched_keywords": matched,
                "blocked_reason": f"sensitive topic(s): {', '.join(sorted(set(blocked)))[:200]}"}
    if len(matched) < max(1, int(min_matches)):
        return {"approved": False, "matched_keywords": matched,
                "blocked_reason": (f"insufficient relevance: {len(matched)} match(es), "
                                   f"need {min_matches}")}
    return {"approved": True, "matched_keywords": matched, "blocked_reason": None}


def _canonical_permalink(article) -> tuple[str, str] | None:
    """Canonical /author/status/id permalink of THIS article.

    Prefers the anchor containing the article's own <time> element (X wraps
    the timestamp in the canonical status link). Falls back to an anchor whose
    href role is 'link' inside the tweet's text group. Quoted tweets live in
    nested articles / show-parent links, which are excluded by construction.
    Returns (href, author) or None."""
    import re as _re
    time_el = None
    try:
        time_el = article.query_selector("time")
    except Exception:
        time_el = None
    if time_el is not None:
        try:
            el = time_el
            for _ in range(8):  # walk up to the wrapping <a>
                handle = el.evaluate_handle("e => e.parentElement")
                el = handle.as_element() if hasattr(handle, "as_element") else None
                if el is None:
                    break
                if el.evaluate("e => e.tagName === 'A'"):
                    href = el.get_attribute("href") or ""
                    m = _re.match(r"^/([^/]+)/status/(\d+)", href)
                    if m:
                        return href, m.group(1)
                    break
        except Exception:
            pass  # fall through to anchor scan
    # fallback: only anchors that belong to THIS article. A quoted tweet renders
    # as its own nested <article>, so any anchor whose closest article differs
    # from the root belongs to the quote/parent content and must be skipped.
    try:
        anchors = article.query_selector_all("a[href*='/status/']")
    except Exception:
        return None
    for a in anchors:
        try:
            same_article = a.evaluate(
                "(e, root) => e.closest('article') === root", article)
        except Exception:
            same_article = True
        if not same_article:
            continue
        href = a.get_attribute("href") or ""
        m = _re.match(r"^/([^/]+)/status/(\d+)$", href)
        if m:
            return href, m.group(1)
    return None


class ScoutEngine:
    """Cookie-authenticated, read-only tweet scout.

    Usage:
        engine = ScoutEngine(cookies={"auth_token": ..., "ct0": ...})
        tweets = engine.search("gm web3", limit=20)
    """

    SEARCH_URL = "https://x.com/search?q={query}&f=live"

    def __init__(self, cookies, keywords: list[str],
                 min_interval_s: int = 240, max_interval_s: int = 720,
                 headless: bool = True):
        # Accepts Playwright-style list-of-dicts or legacy {name: value} map.
        if isinstance(cookies, dict):
            cookies = [{"name": k, "value": v, "domain": ".x.com", "path": "/"}
                       for k, v in cookies.items()]
        names = {c.get("name") for c in cookies}
        missing = {"auth_token", "ct0"} - names
        if missing:
            raise ValueError(f"missing required cookie(s): {', '.join(sorted(missing))}")
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
            ctx.add_cookies(self.cookies)  # already Playwright-shaped
            page = ctx.new_page()
            page.goto(self.SEARCH_URL.format(query=query), wait_until="domcontentloaded")
            page.wait_for_selector("article[data-testid='tweet']", timeout=15000)
            page.wait_for_timeout(2000)
            articles = page.query_selector_all("article[data-testid='tweet']")
            seen: set[str] = set()
            for art in articles[:limit]:
                try:
                    # All fields must come from the SAME article element.
                    text_el = art.query_selector("div[data-testid='tweetText']")
                    text = text_el.inner_text() if text_el else ""
                    canon = _canonical_permalink(art)
                    if not canon:
                        continue  # no trustworthy permalink -> reject
                    href, author = canon
                    tid = href.rstrip("/").rsplit("/", 1)[-1]
                    # consistency: the article's own text must belong to this id's
                    # conversation; quoted content lives in a nested article
                    nested = art.query_selector("article[data-testid='tweet'] article[data-testid='tweet']")
                    if nested is not None and not re.match(rf"^/{re.escape(author)}/status/", href or ""):
                        continue
                    if not tid or not author or tid in seen:
                        continue
                    seen.add(tid)
                    imgs = art.query_selector_all("img[src*='pbs.twimg.com/media']")
                    candidates.append(TweetCandidate(
                        id=tid, author=author, text=text.strip(),
                        url=f"{base_url}{href}",
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
