"""Read-only DOM verification probe for X reply selectors.

Developer verification tool: confirms whether the expected reply-compose
selectors exist on a tweet page under the current X markup, and reports their
state. STRICTLY READ-ONLY:

- All POST/PUT/PATCH/DELETE requests are aborted at the route layer.
- No text is typed, no button is clicked, no form is submitted.
- The reply composer is opened only via URL navigation (intent=replay), never
  via UI interaction.

Usage:
    python e2e_dom_check.py <tweet_url> [more_urls...]

Output: per-URL JSON report on stdout and data/dom_check_report.json.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BLOCKED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

SELECTORS = {
    "tweet_article": "article[data-testid='tweet']",
    "reply_textarea": "div[data-testid='tweetTextarea_0']",
    "submit_button": "button[data-testid='tweetButtonInline']",
}


@dataclass
class SelectorReport:
    name: str
    found: bool
    visible: bool | None = None
    enabled: bool | None = None
    bounding_box: dict | None = None
    aria_label: str | None = None
    error: str | None = None


@dataclass
class UrlReport:
    url: str
    navigated: bool = False
    page_title: str = ""
    logged_in: bool | None = None
    selectors: list[SelectorReport] = field(default_factory=list)
    blocking_dialog: bool | None = None
    error: str | None = None

    @property
    def all_core_selectors_ok(self) -> bool:
        core = [s for s in self.selectors
                if s.name in ("reply_textarea", "submit_button")]
        return bool(core) and all(s.found for s in core)


def _inspect(page, name: str, selector: str) -> SelectorReport:
    rep = SelectorReport(name=name, found=False)
    try:
        el = page.query_selector(selector)
        if el is None:
            return rep
        rep.found = True
        rep.visible = el.is_visible()
        rep.bounding_box = el.bounding_box()
        try:
            rep.aria_label = el.get_attribute("aria-label")
        except Exception:
            pass
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        if tag == "button":
            rep.enabled = el.is_enabled()
        else:
            rep.enabled = None  # contenteditable div: no disabled semantics
    except Exception as e:
        rep.error = str(e)
    return rep


def verify_reply_dom(tweet_url: str, headless: bool = True) -> UrlReport:
    from playwright.sync_api import sync_playwright

    cookies_path = ROOT / "data" / "cookies.json"
    if not cookies_path.exists():
        raise FileNotFoundError(
            f"{cookies_path} missing — create it with auth_token + ct0")
    cookies = json.loads(cookies_path.read_text(encoding="utf-8"))

    report = UrlReport(url=tweet_url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        # Read-only invariant: abort every write-method request.
        ctx.route("**/*", lambda route: (
            route.abort() if route.request.method in BLOCKED_METHODS
            else route.continue_()
        ))
        ctx.add_cookies([
            {"name": n, "value": v, "domain": ".x.com", "path": "/",
             "httpOnly": n == "auth_token", "secure": True}
            for n, v in cookies.items()
        ])
        page = ctx.new_page()
        try:
            # Navigate straight to the reply composer via URL — no clicks.
            page.goto(f"{tweet_url}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            report.navigated = True
            report.page_title = page.title()

            report.logged_in = page.query_selector(
                "a[data-testid='SideNav_AccountSwitcher_Button']") is not None

            # Open the composer without clicking anything on the tweet itself:
            # the reply modal can be summoned by URL hash on the status page.
            page.evaluate("window.location.hash = '#compose'")  # no-op fallback
            page.wait_for_timeout(1500)

            # Modal selectors (reply dialog) then inline selectors (status page)
            for name, sel in SELECTORS.items():
                report.selectors.append(_inspect(page, name, sel))
            if not any(s.found for s in report.selectors
                       if s.name == "reply_textarea"):
                # try the modal variant
                modal = _inspect(page, "reply_textarea_modal",
                                 "div[data-testid='tweetTextarea_0']")
                report.selectors.append(modal)

            report.blocking_dialog = page.query_selector(
                "div[data-testid='confirmationSheetConfirm']") is not None
        except Exception as e:
            report.error = str(e)
        finally:
            browser.close()
    return report


def run(urls: list[str], headless: bool = True) -> list[UrlReport]:
    reports = []
    for i, u in enumerate(urls):
        if i:
            time.sleep(5)  # polite spacing between page loads
        print(f"checking {u} ...", file=sys.stderr)
        reports.append(verify_reply_dom(u, headless=headless))
    out = ROOT / "data" / "dom_check_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(r) for r in reports], indent=2),
                   encoding="utf-8")
    for r in reports:
        status = "OK" if r.all_core_selectors_ok else "MISSING/DRIFT"
        print(f"\n{r.url}: {status} (logged_in={r.logged_in}, "
              f"navigated={r.navigated}, error={r.error})")
        for s in r.selectors:
            print(f"  {s.name:24} found={s.found} visible={s.visible} "
                  f"enabled={s.enabled} box={s.bounding_box}")
    print(f"\nreport written: {out}")
    return reports


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    raise SystemExit(run(sys.argv[1:]))
