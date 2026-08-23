"""Regression tests for the gork political-tweet incident.

Observed failure: a politics/unions/violence tweet passed the old score gate
and got an irrelevant "web3 keeps showing up..." reply; report flags were
false-negatives due to missing waits.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

from scout import evaluate_candidate, DEFAULT_BLOCKED_TERMS, _canonical_permalink  # noqa: E402
from runner import delivery_allowed  # noqa: E402

POLITICAL_TWEET = (
    "web3 keeps showing up in my feed today. unions are organizing, there was "
    "violence at the protest, and the election is next month. politics is war "
    "by other means"
)
RELEVANT_TWEET = "gm everyone building on web3 today, crypto rails keep improving"

KEYWORDS = ["gm", "web3", "crypto", "bitcoin", "eth", "solana", "ai agents"]


def test_political_tweet_rejected():
    gate = evaluate_candidate(POLITICAL_TWEET, KEYWORDS,
                              min_matches=1, blocked_terms=DEFAULT_BLOCKED_TERMS)
    assert not gate["approved"]
    assert "sensitive topic" in gate["blocked_reason"]


def test_relevant_web3_tweet_passes():
    gate = evaluate_candidate(RELEVANT_TWEET, KEYWORDS,
                              min_matches=1, blocked_terms=DEFAULT_BLOCKED_TERMS)
    assert gate["approved"], gate["blocked_reason"]
    assert "gm" in gate["matched_keywords"] and "web3" in gate["matched_keywords"]


def test_min_match_count_enforced():
    only_one = "check out this gm post"
    gate = evaluate_candidate(only_one, KEYWORDS, min_matches=2,
                              blocked_terms=DEFAULT_BLOCKED_TERMS)
    assert not gate["approved"]


def test_keyword_and_block_terms_use_word_boundaries():
    gate = evaluate_candidate("we build together after the reunion", KEYWORDS,
                              min_matches=1, blocked_terms=["union"])
    assert not gate["approved"]
    assert gate["matched_keywords"] == []
    assert "sensitive topic" not in gate["blocked_reason"]


def test_blocked_overrides_score_and_image():
    # contains 4 keywords AND an image bonus but must still be blocked
    sneaky = "gm web3 crypto bitcoin solana — time for war"
    gate = evaluate_candidate(sneaky, KEYWORDS, min_matches=1,
                              blocked_terms=DEFAULT_BLOCKED_TERMS)
    assert not gate["approved"] and "war" in gate["blocked_reason"]


def test_quoted_link_does_not_replace_canonical():
    class FakeEl:
        def __init__(self, href, same_article):
            self._href = href
            self._same = same_article  # models closest('article') === root
        def get_attribute(self, name): return self._href if name == "href" else None
        def evaluate(self, script, arg=None): return self._same

    class FakeHandle:
        def as_element(self): return None  # time not wrapped in <a> in this fake

    class FakeTime:
        def evaluate_handle(self, *a): return FakeHandle()

    class Article:
        def query_selector(self, sel): return FakeTime() if sel == "time" else None
        def query_selector_all(self, sel):
            # first link belongs to a nested quoted article; second is canonical
            return [FakeEl("/quoteduser/status/111", False),
                    FakeEl("/realauthor/status/2091405577872114073", True)]

    canon = _canonical_permalink(Article())
    assert canon == ("/realauthor/status/2091405577872114073", "realauthor"), canon


def test_reply_agent_gates_before_fill_and_click(tmp_path=None):
    """tweet_loaded/login false => no fill, no click, failed result."""
    from reply_agent import ReplyAgent
    import reply_agent as ra

    COOKIES = [{"name": "auth_token", "value": "S-A"}, {"name": "ct0", "value": "S-B"}]

    class FakeLocator:
        first = None
        def __init__(self): self.first = self
        filled = False
        clicked = False
        def wait_for(self, **k): pass
        def fill(self, t): type(self).filled = t
        def is_enabled(self): return True
        def click(self): type(self).clicked = True

    class Page:
        waits = []
        def wait_for_selector(self, sel, **k):
            Page.waits.append(sel)
            raise TimeoutError("never appears")  # simulate loading screen forever
        def locator(self, sel): return FakeLocator()
        def screenshot(self, **k): pass
        def goto(self, *a, **k): pass

    class Ctx:
        def __init__(self, p): self._p = p
        def add_cookies(self, c): pass
        def new_page(self): return self._p

    class Browser:
        def __init__(self, c): self._c = c
        def new_context(self, **k): return self._c
        def close(self): pass

    class PW:
        chromium = None
        def __init__(self): PW.chromium = self
        def launch(self, **k): return Browser(Ctx(Page()))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    agent = ReplyAgent(cookies=COOKIES, dry_run=True,
                       artifacts_dir=Path(tempfile.mkdtemp()) / "art")
    orig = ra.sync_playwright
    ra.sync_playwright = lambda: PW()
    try:
        result = agent.reply("https://x.com/gork/status/2091405577872114073",
                             "irrelevant draft text",
                             report_extra={"draft_source": "local_fallback"})
        assert result.status == "failed"
        assert not FakeLocator.filled, "textarea was filled despite gate"
        assert not FakeLocator.clicked, "send was clicked"
        r = result.report
        assert r["tweet_loaded"] is False and r["login_detected"] is False
        assert r["send_clicked"] is False
        assert r["draft_source"] == "local_fallback"
        assert r["error"] and "did not load" in r["error"] or "login" in (r["error"] or "")
    finally:
        ra.sync_playwright = orig


def test_default_blocked_terms_apply_when_config_is_null():
    configured = None
    terms = DEFAULT_BLOCKED_TERMS if configured is None else configured
    gate = evaluate_candidate(POLITICAL_TWEET, KEYWORDS, 1, terms)
    assert not gate["approved"] and "violence" in gate["blocked_reason"]


def test_no_provider_blocks_before_live_delivery():
    assert delivery_allowed("local_fallback", dry_run=True)
    assert not delivery_allowed("local_fallback", dry_run=False)
    assert delivery_allowed("llm", dry_run=False)


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
