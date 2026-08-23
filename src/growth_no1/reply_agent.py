"""Playwright-powered X reply delivery with dry-run and duplicate-safe callers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReplyResult:
    ok: bool
    status: str
    error: str | None = None


class ReplyAgent:
    TEXTAREA = "div[data-testid='tweetTextarea_0']"
    SEND_BUTTON = "button[data-testid='tweetButtonInline']"

    def __init__(self, cookies: dict[str, str], headless: bool = True,
                 dry_run: bool = True, timeout_ms: int = 30_000):
        if "auth_token" not in cookies or "ct0" not in cookies:
            raise ValueError("both auth_token and ct0 cookies required")
        self.cookies = cookies
        self.headless = headless
        self.dry_run = dry_run
        self.timeout_ms = timeout_ms

    def reply(self, tweet_url: str, text: str) -> ReplyResult:
        if not text.strip():
            return ReplyResult(False, "failed", "empty reply")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            ctx.add_cookies([
                {"name": name, "value": value, "domain": ".x.com", "path": "/",
                 "httpOnly": name == "auth_token", "secure": True}
                for name, value in self.cookies.items()
            ])
            page = ctx.new_page()
            try:
                page.goto(tweet_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                textarea = page.locator(self.TEXTAREA).first
                textarea.wait_for(state="visible", timeout=self.timeout_ms)
                textarea.fill(text.strip())
                button = page.locator(self.SEND_BUTTON).first
                button.wait_for(state="visible", timeout=self.timeout_ms)
                if not button.is_enabled():
                    return ReplyResult(False, "failed", "reply button is disabled")
                if self.dry_run:
                    return ReplyResult(True, "dry_run")

                # The only mutating UI action in this module.
                with page.expect_response(
                    lambda r: "CreateTweet" in r.url and r.request.method == "POST",
                    timeout=self.timeout_ms,
                ) as response_info:
                    button.click()
                response = response_info.value
                if not response.ok:
                    return ReplyResult(False, "failed", f"X returned HTTP {response.status}")
                return ReplyResult(True, "posted")
            except Exception as exc:
                return ReplyResult(False, "failed", str(exc))
            finally:
                browser.close()
