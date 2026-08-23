"""Mocked/unit tests for dry-run evidence invariants."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

import reply_agent as ra  # noqa: E402
from reply_agent import ReplyAgent, ReplyResult  # noqa: E402


COOKIES = [{"name": "auth_token", "value": "SECRET-A"},
           {"name": "ct0", "value": "SECRET-B"}]
URL = "https://x.com/someone/status/1234567890"


class FakeLocator:
    def __init__(self, page): self._page = page; self.first = self
    def wait_for(self, state=None, timeout=None): pass
    def fill(self, text): self._page.filled = text
    def is_enabled(self): return True
    def click(self):
        # Any click in dry run is a test failure.
        if getattr(self._page, "dry_run_mode", False):
            raise AssertionError("CLICK EXECUTED IN DRY RUN")
        self._page.clicked = True


class FakePage:
    def __init__(self, dry_run_mode=True):
        self.dry_run_mode = dry_run_mode
        self.clicked = False
        self.filled = None
        self.screenshots = []
    def query_selector(self, sel): return object() if "tweet'" in sel or "SideNav" in sel else None
    def locator(self, sel): return FakeLocator(self)
    def screenshot(self, path=None, full_page=False): self.screenshots.append(path); return Path(path).write_bytes(b"\x89PNG fake")
    def goto(self, *a, **k): pass


class FakeCtx:
    def __init__(self, page): self._page = page
    def add_cookies(self, c): self.cookies_added = c
    def new_page(self): return self._page


class FakeBrowser:
    def __init__(self, ctx): self._ctx = ctx
    def new_context(self, **k): return self._ctx
    def close(self): pass


class FakePW:
    def __init__(self, browser):
        self._b = browser
        self.chromium = self  # real API: p.chromium.launch(...)
    def launch(self, **k): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_sync_playwright(browser):
    """Mimics playwright.sync_api.sync_playwright: called, then used as ctx mgr."""
    pw = FakePW(browser)
    return lambda: pw


def _agent(tmp: Path) -> tuple[ReplyAgent, FakePage]:
    agent = ReplyAgent(cookies=COOKIES, dry_run=True,
                       artifacts_dir=tmp / "artifacts" / "playwright")
    page = FakePage(dry_run_mode=True)
    return agent, page


def test_dry_run_never_clicks_send(tmp_path):
    agent, page = _agent(Path(tempfile.mkdtemp()))
    orig = ra.sync_playwright
    ra.sync_playwright = fake_sync_playwright(FakeBrowser(FakeCtx(page)))
    try:
        result = agent.reply(URL, "gm test reply")
        assert result.ok and result.status == "dry_run"
        assert not page.clicked, "send button was clicked during dry run"
    finally:
        ra.sync_playwright = orig


def test_report_send_clicked_false_and_fields(tmp_path):
    agent, page = _agent(Path(tempfile.mkdtemp()))
    orig = ra.sync_playwright
    ra.sync_playwright = fake_sync_playwright(FakeBrowser(FakeCtx(page)))
    try:
        result = agent.reply(URL, "gm test reply")
        r = result.report
        assert r["send_clicked"] is False
        for field in ("tweet_url", "login_detected", "tweet_loaded",
                      "textarea_found", "text_filled", "send_button_found",
                      "send_button_enabled"):
            assert field in r
        assert r["tweet_url"] == URL
        assert r["text_filled"] is True
        # report file exists on disk and parses
        disk = json.loads((Path(result.evidence_dir) / "report.json").read_text())
        assert disk["send_clicked"] is False
    finally:
        ra.sync_playwright = orig


def test_artifacts_contain_no_secrets(tmp_path):
    agent, page = _agent(Path(tempfile.mkdtemp()))
    orig = ra.sync_playwright
    ra.sync_playwright = fake_sync_playwright(FakeBrowser(FakeCtx(page)))
    try:
        result = agent.reply(URL, "some reply text")
        d = Path(result.evidence_dir)
        blob = "\n".join(f.read_text(errors="ignore") for f in d.iterdir() if f.suffix == ".json")
        assert "SECRET-A" not in blob and "SECRET-B" not in blob
        assert "auth_token=" not in blob and "ct0=" not in blob
        # screenshots are png bytes only; no json inside
        png = (d / "01-tweet-loaded.png").read_bytes()
        assert b"SECRET" not in png
        # error sanitization: inject a fake leaky error
        sanitized = ra._sanitize_error(RuntimeError(
            "failed at auth_token=SECRET-A with api_key=sk-lambda-xyz"))
        assert "SECRET-A" not in sanitized and "sk-lambda-xyz" not in sanitized
        quoted = ra._sanitize_error(
            RuntimeError('request payload {"auth_token": "SECRET-A"}'),
            ("SECRET-A", "SECRET-B"))
        assert "SECRET-A" not in quoted
    finally:
        ra.sync_playwright = orig


def test_evidence_failure_does_not_cause_posting(tmp_path):
    """If evidence writing explodes, the flow must still NOT click send."""
    agent, page = _agent(Path(tempfile.mkdtemp()))
    agent.artifacts_dir = tmp_path  # will be made read-hostile below
    # make evidence dir creation fail
    class BoomDir:
        def __truediv__(self, other): raise OSError("disk full")
    agent._new_evidence_dir = lambda url: (_ for _ in ()).throw(OSError("evidence io failure"))
    orig = ra.sync_playwright
    ra.sync_playwright = fake_sync_playwright(FakeBrowser(FakeCtx(page)))
    try:
        result = agent.reply(URL, "gm")
        assert result.status == "failed"
        assert "evidence io failure" in result.error
        assert not page.clicked
    finally:
        ra.sync_playwright = orig


def test_live_mode_still_clicks(tmp_path):
    """Sanity: non-dry-run mode retains its (pre-existing) click path."""
    agent = ReplyAgent(cookies=COOKIES, dry_run=False,
                       artifacts_dir=Path(tempfile.mkdtemp()) / "art")
    page = FakePage(dry_run_mode=False)

    class FakeRespInfo:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        value = type("R", (), {"ok": True})()
    page.expect_response = lambda *a, **k: FakeRespInfo()

    orig = ra.sync_playwright
    ra.sync_playwright = fake_sync_playwright(FakeBrowser(FakeCtx(page)))
    try:
        result = agent.reply(URL, "live text")
        assert result.status == "posted"
        assert page.clicked
    finally:
        ra.sync_playwright = orig


if __name__ == "__main__":
    failures = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        tmp = tempfile.mkdtemp()
        try:
            fn(Path(tmp) / "d") if fn.__code__.co_argcount else fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
