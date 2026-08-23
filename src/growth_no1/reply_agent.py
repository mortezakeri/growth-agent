"""Playwright-powered X reply delivery with dry-run and duplicate-safe callers.

Dry runs capture sanitized visual evidence (screenshots + JSON report) under
artifacts/playwright/<tweet-id>-<timestamp>/. Evidence never includes cookie
values, auth headers, storage state, or full traces — only screenshots of the
target tweet/composer and a boolean report.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright  # noqa: F401 - patched in tests
except ImportError:  # allows unit tests to run without playwright installed
    sync_playwright = None

ROOT = Path(__file__).resolve().parent.parent.parent
ARTIFACTS_DIR = ROOT / "artifacts" / "playwright"


@dataclass
class ReplyResult:
    ok: bool
    status: str
    error: str | None = None
    evidence_dir: str | None = None
    report: dict = field(default_factory=dict)


def _tweet_id(tweet_url: str) -> str:
    m = re.search(r"/status/(\d+)", tweet_url or "")
    return m.group(1) if m else "unknown"


def _sanitize_error(exc: Exception, sensitive_values=()) -> str:
    """Error text with any cookie-like/key-like substrings stripped."""
    msg = str(exc)
    msg = re.sub(r"(auth_token|ct0|api[_-]?key|token)\s*[=:]?\s*\S+", r"\1=<redacted>", msg, flags=re.I)
    for value in sensitive_values:
        if value:
            msg = msg.replace(str(value), "<redacted>")
    return msg[:500]


class ReplyAgent:
    TEXTAREA = "div[data-testid='tweetTextarea_0']"
    SEND_BUTTON = "button[data-testid='tweetButtonInline']"

    def __init__(self, cookies, headless: bool = True,
                 dry_run: bool = True, timeout_ms: int = 30_000,
                 artifacts_dir: Path | None = None):
        # Accepts Playwright-style list-of-dicts or legacy {name: value} map.
        if isinstance(cookies, dict):
            cookies = [{"name": k, "value": v, "domain": ".x.com", "path": "/",
                        "httpOnly": k == "auth_token", "secure": True}
                       for k, v in cookies.items()]
        names = {c.get("name") for c in cookies}
        missing = {"auth_token", "ct0"} - names
        if missing:
            raise ValueError(f"missing required cookie(s): {', '.join(sorted(missing))}")
        self.cookies = cookies
        self.headless = headless
        self.dry_run = dry_run
        self.timeout_ms = timeout_ms
        self.artifacts_dir = artifacts_dir or ARTIFACTS_DIR

    # ---------------------------------------------------------- evidence

    def _new_evidence_dir(self, tweet_url: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        d = self.artifacts_dir / f"{_tweet_id(tweet_url)}-{stamp}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_report(self, d: Path, tweet_url: str, **flags) -> dict:
        report = {
            "tweet_url": tweet_url,
            "login_detected": flags.get("login_detected", False),
            "tweet_loaded": flags.get("tweet_loaded", False),
            "textarea_found": flags.get("textarea_found", False),
            "text_filled": flags.get("text_filled", False),
            "send_button_found": flags.get("send_button_found", False),
            "send_button_enabled": flags.get("send_button_enabled", None),
            "send_clicked": False,  # invariant: dry run never clicks
            "error": (_sanitize_error(flags["error"],
                                      (c.get("value") for c in self.cookies))
                      if flags.get("error") else None),
            "dry_run": self.dry_run,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (d / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report

    def reply(self, tweet_url: str, text: str) -> ReplyResult:
        if not text.strip():
            return ReplyResult(False, "failed", "empty reply")

        if sync_playwright is None:
            return ReplyResult(False, "failed", "Playwright is not installed")
        try:
            evidence = self._new_evidence_dir(tweet_url) if self.dry_run else None
        except Exception as exc:
            return ReplyResult(False, "failed", _sanitize_error(
                exc, (c.get("value") for c in self.cookies)))
        report_kwargs: dict = {}

        def finish(ok: bool, status: str, error: Exception | None = None) -> ReplyResult:
            report = None
            if evidence is not None:
                try:
                    report = self._write_report(evidence, tweet_url,
                                                error=error, **report_kwargs)
                except Exception as evidence_exc:
                    if error is None:
                        error = evidence_exc
            return ReplyResult(ok, status,
                               error=(_sanitize_error(
                                   error, (c.get("value") for c in self.cookies))
                                      if error else None),
                               evidence_dir=str(evidence) if evidence else None,
                               report=report or {})

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            ctx.add_cookies(self.cookies)  # already Playwright-shaped
            page = ctx.new_page()
            try:
                page.goto(tweet_url, wait_until="domcontentloaded",
                          timeout=self.timeout_ms)
                report_kwargs["login_detected"] = page.query_selector(
                    "a[data-testid='SideNav_AccountSwitcher_Button']") is not None
                article_ok = page.query_selector("article[data-testid='tweet']") is not None
                report_kwargs["tweet_loaded"] = article_ok
                if evidence:
                    page.screenshot(path=str(evidence / "01-tweet-loaded.png"),
                                    full_page=False)

                textarea = page.locator(self.TEXTAREA).first
                textarea.wait_for(state="visible", timeout=self.timeout_ms)
                report_kwargs["textarea_found"] = True
                textarea.fill(text.strip())
                report_kwargs["text_filled"] = True
                if evidence:
                    page.screenshot(path=str(evidence / "02-reply-filled.png"),
                                    full_page=False)

                button = page.locator(self.SEND_BUTTON).first
                button.wait_for(state="visible", timeout=self.timeout_ms)
                report_kwargs.update(send_button_found=True,
                                     send_button_enabled=button.is_enabled())
                if not button.is_enabled():
                    return finish(False, "failed",
                                  RuntimeError("reply button is disabled"))
                if self.dry_run:
                    # Invariant: stop here. No click, no CreateTweet POST.
                    return finish(True, "dry_run")

                # The only mutating UI action in this module.
                with page.expect_response(
                    lambda r: "CreateTweet" in r.url and r.request.method == "POST",
                    timeout=self.timeout_ms,
                ) as response_info:
                    button.click()
                response = response_info.value
                if not response.ok:
                    return finish(False, "failed",
                                  RuntimeError(f"X returned HTTP {response.status}"))
                return finish(True, "posted")
            except Exception as exc:
                return finish(False, "failed", exc)
            finally:
                browser.close()
