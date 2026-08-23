"""Tests for cookie parsing, provider fallback/masking, secrets persistence."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

import cookies as cookie_store  # noqa: E402
import runtime_config as rc  # noqa: E402
import nous_client  # noqa: E402


def test_editor_v3_array():
    raw = [
        {"name": "auth_token", "value": "aaa", "domain": ".x.com",
         "path": "/", "httpOnly": True, "secure": True,
         "sameSite": "no_restriction", "expirationDate": 1790000000.5},
        {"name": "ct0", "value": "bbb", "domain": ".x.com",
         "sameSite": "lax"},
    ]
    out = cookie_store.parse_cookies(raw)
    assert {c["name"] for c in out} == {"auth_token", "ct0"}
    at = next(c for c in out if c["name"] == "auth_token")
    assert at["sameSite"] == "None" and at["expires"] == 1790000000
    assert at["httpOnly"] is True and at["secure"] is True


def test_simple_object():
    out = cookie_store.parse_cookies('{"auth_token": "x", "ct0": "y"}')
    assert len(out) == 2
    d = cookie_store.as_simple_dict(out)
    assert d == {"auth_token": "x", "ct0": "y"}


def test_missing_required():
    for bad in ([{"name": "ct0", "value": "y"}],
                [{"name": "auth_token", "value": "x"}], []):
        try:
            cookie_store.parse_cookies(bad)
            raise AssertionError("should have raised")
        except cookie_store.CookieError as e:
            assert "aaa" not in str(e) and "bbb" not in str(e)  # no values leaked
            assert "missing required cookie" in str(e)


def test_samesite_normalization():
    assert cookie_store._normalize_same_site("no_restriction") == "None"
    assert cookie_store._normalize_same_site("strict") == "Strict"
    assert cookie_store._normalize_same_site("unspecified") == "Lax"
    assert cookie_store._normalize_same_site(None) is None
    try:
        cookie_store._normalize_same_site("banana")
        raise AssertionError("should have raised")
    except cookie_store.CookieError:
        pass


def test_env_and_fallback(tmp_path=None):
    os.environ["X_COOKIES_JSON"] = '[{"name":"auth_token","value":"a"},{"name":"ct0","value":"b"}]'
    got = cookie_store.load_cookie_source()
    assert len(got) == 2
    os.environ["X_COOKIES_JSON"] = "not json {{{"
    try:
        cookie_store.load_cookie_source()
        raise AssertionError("should have raised")
    except cookie_store.CookieError as e:
        assert "not valid JSON" in str(e)
    del os.environ["X_COOKIES_JSON"]
    # missing both sources -> clear error naming the env var and path
    saved = cookie_store.ROOT
    try:
        cookie_store.ROOT = Path(tempfile.mkdtemp())  # no data/cookies.json inside
        try:
            cookie_store.load_cookie_source()
            raise AssertionError("should have raised")
        except cookie_store.CookieError as e:
            assert "X_COOKIES_JSON" in str(e) and "cookies.json" in str(e)
    finally:
        cookie_store.ROOT = saved


def test_provider_fallback_no_key(monkeypatch_dict=None):
    # ensure no key anywhere
    saved = dict(os.environ)
    for k in list(os.environ):
        if "API_KEY" in k:
            del os.environ[k]
    original_secrets = rc.SECRETS_PATH
    rc.SECRETS_PATH = Path(tempfile.mkdtemp()) / "secrets.json"
    try:
        key, source = rc.get_active_api_key()
        assert key is None and source == "none"
        try:
            nous_client._resolve_key_and_endpoint()
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "no API key" in str(e)
        # generate_drafts must not crash without a provider (local fallback)
        out = nous_client.generate_drafts("vitalik", "gm web3", ("gm",))
        assert "gm" in out["gm"].lower()
    finally:
        rc.SECRETS_PATH = original_secrets
        os.environ.clear()
        os.environ.update(saved)


def test_masking_and_secrets_persistence(tmp_path=None):
    assert rc.mask_key("") == "(not set)"
    assert rc.mask_key("short") == "(set)"
    m = rc.mask_key("sk-abcdef1234567890")
    assert m.startswith("sk-a") and m.endswith("7890") and "…" in m
    assert "abcdef123456" not in m

    original_settings_path = rc.SETTINGS_PATH
    original_secrets_path = rc.SECRETS_PATH
    sandbox = Path(tempfile.mkdtemp())
    rc.SETTINGS_PATH = sandbox / "settings.json"
    rc.SECRETS_PATH = sandbox / "secrets.json"
    try:
        cfg = rc.set_provider("gemini", "test-key-gemini-123456789")
        p = cfg["provider"]
        assert p["name"] == "gemini"
        assert "generativelanguage.googleapis.com" in p["endpoint"]
        assert p["model"] == "gemini-3.6-flash"
        assert p["api_key_env"] == "GEMINI_API_KEY"
        # key persisted ONLY in gitignored secrets file
        assert "test-key-gemini" not in rc.SETTINGS_PATH.read_text(encoding="utf-8")
        secrets = json.loads(rc.SECRETS_PATH.read_text(encoding="utf-8"))
        assert secrets["gemini"] == "test-key-gemini-123456789"
        key, source = rc.get_active_api_key()
        assert key == "test-key-gemini-123456789" and source == "secrets.json"
        summary = rc.masked_summary()
        assert "test-key-gemini" not in summary and "…6789" in summary.replace("\n", "")
        print(f"   gemini endpoint: {p['endpoint']}")
    finally:
        rc.SETTINGS_PATH = original_settings_path
        rc.SECRETS_PATH = original_secrets_path


if __name__ == "__main__":
    import tempfile
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
