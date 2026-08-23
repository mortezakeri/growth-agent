"""Regression tests for runtime_config.py security hardening.

Proves:
- Pre-existing unknown secret fields in cloud_runtime.json are cleaned on the
  next save (TOCTOU fix).
- Secret-like nested metrics cannot persist (metrics removed entirely).
- Invalid update_offset does not crash and becomes 0.
- paused="false" does not become True (string truthiness rejected).
- Existing valid cloud behavior still works.
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

import runtime_config as rc  # noqa: E402


@contextmanager
def runtime_isolation() -> SimpleNamespace:
    """Isolate CLOUD_RUNTIME_PATH, SETTINGS_PATH, SECRETS_PATH for these tests.

    Restores every path and env on exit, even on assertion failure.
    """
    sandbox = Path(tempfile.mkdtemp())
    (sandbox / "config").mkdir()
    (sandbox / "data").mkdir()
    saved = {
        "settings": rc.SETTINGS_PATH,
        "cloud": rc.CLOUD_RUNTIME_PATH,
        "secrets": rc.SECRETS_PATH,
    }
    rc.SETTINGS_PATH = sandbox / "config" / "settings.json"
    rc.CLOUD_RUNTIME_PATH = sandbox / "data" / "cloud_runtime.json"
    rc.SECRETS_PATH = sandbox / "config" / "secrets.json"
    rc.SETTINGS_PATH.write_text(json.dumps({
        "working_windows": [
            {"name": "morning", "start": "06:00", "end": "12:00", "max_replies": 5},
            {"name": "evening", "start": "12:30", "end": "01:00",
             "crosses_midnight": True, "max_replies": 5}],
        "drafts": {"skill_prompt": None, "style_override": None},
        "provider": {"name": "nous", "endpoint": "https://x/v1",
                     "model": "m", "api_key_env": "NOUS_API_KEY"},
    }), encoding="utf-8")
    try:
        yield SimpleNamespace(sandbox=sandbox)
    finally:
        rc.SETTINGS_PATH = saved["settings"]
        rc.CLOUD_RUNTIME_PATH = saved["cloud"]
        rc.SECRETS_PATH = saved["secrets"]


def _seed_cloud(raw: dict) -> None:
    """Write raw JSON directly into cloud_runtime.json (bypasses save_cloud_state)."""
    rc.CLOUD_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    rc.CLOUD_RUNTIME_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _read_cloud() -> dict:
    """Read the current cloud_runtime.json if it exists and is valid JSON."""
    try:
        return json.loads(rc.CLOUD_RUNTIME_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Regression: pre-existing unknown secret fields are cleaned on next save
# ---------------------------------------------------------------------------

def test_existing_unknown_keys_cleaned_on_save() -> None:
    """Inject cookies / api_key / authorization / arbitrary key directly into the
    file, then call save_cloud_state with a benign update. The evil keys must be
    gone after the write."""
    _seed_cloud({
        "update_offset": 5,
        "paused": False,
        "cookies": {"_test_session": "stolen-cookie"},
        # use obviously-test-only key names so a naive scanner doesn't flag them
        "_test_injection_api_key": "_test-sk-12345",
        "_test_injection_auth": "Bearer _test-leaked",
        "bot_token": "_test-telegram-bot-secret",
        "evil_unknown_key": "nope",
        "last_run": "2026-08-23T10:00:00+00:00",
    })
    rc.save_cloud_state(update_offset=6)
    blob = rc.CLOUD_RUNTIME_PATH.read_text(encoding="utf-8")
    for bad in ("cookies", "_test_injection_api_key", "_test_injection_auth",
                "bot_token", "evil_unknown_key", "_test_session", "stolen-cookie",
                "_test-sk-12345", "Bearer _test-leaked", "_test-telegram-bot-secret"):
        assert bad not in blob, f"forbidden value leaked: {bad}"
    state = _read_cloud()
    assert state["update_offset"] == 6
    assert state["paused"] is False
    assert "last_run" in state
    assert "updated_at" in state


def test_existing_metric_dict_cleaned_on_save() -> None:
    """If an old cloud_runtime.json carried a 'metrics' dict, the next save must
    drop it entirely."""
    _seed_cloud({
        "update_offset": 0,
        "metrics": {
            "evaluated": 10,
            "drafts": 3,
            "errors": 0,
            "day": "2026-08-23",
            "_test_injection_secret_payload": "cannot hide here",
        },
    })
    rc.save_cloud_state(update_offset=1)
    assert "metrics" not in _read_cloud(), "metrics key must be removed"
    assert _read_cloud()["update_offset"] == 1


# ---------------------------------------------------------------------------
# Regression: metrics cannot persist secret-like nested values
# ---------------------------------------------------------------------------

def test_metrics_key_absent_from_allowlist() -> None:
    """metrics must no longer be in _CLOUD_SAFE_KEYS, so even a fresh
    save_cloud_state(metrics=...) drops it and writes only safe fields."""
    with runtime_isolation():
        rc.save_cloud_state(metrics={"_test_injection_evil": "secret",
                                  "nested": {"_k": "v"}})
        state = _read_cloud()
        assert "metrics" not in state, "metrics must be silently dropped"
        assert "updated_at" in state


# ---------------------------------------------------------------------------
# Regression: invalid update_offset does not crash
# ---------------------------------------------------------------------------

def test_invalid_update_offset_defaults_to_zero() -> None:
    """String, bool, float out of range, None, list, dict — all must yield 0
    without throwing."""
    with runtime_isolation():
        cases = [
            {"update_offset": "not-an-int"},
            {"update_offset": True},
            {"update_offset": False},
            {"update_offset": -5},
            {"update_offset": 3.7},
            {"update_offset": None},
            {"update_offset": []},
            {"update_offset": {}},
            {"update_offset": "   "},
        ]
        for payload in cases:
            _seed_cloud(payload)
            state = rc.load_cloud_state()
            assert state["update_offset"] == 0, \
                f"payload {payload!r} should yield 0, got {state['update_offset']!r}"


def test_valid_update_offset_still_works() -> None:
    """Only a legitimate non-negative integer offset survives."""
    _seed_cloud({"update_offset": 42})
    assert rc.load_cloud_state()["update_offset"] == 42
    _seed_cloud({"update_offset": "99"})
    assert rc.load_cloud_state()["update_offset"] == 0


def test_existing_nested_secret_fields_cleaned_on_save() -> None:
    _seed_cloud({
        "drafts": {"skill_prompt": "safe", "style_override": "witty",
                   "api_key": "_test-secret"},
        "working_windows": [{"name": "morning", "start": "06:00",
                             "end": "12:00", "max_replies": 2,
                             "cookies": "_test-cookie"}],
    })
    rc.save_cloud_state(paused=False)
    blob = rc.CLOUD_RUNTIME_PATH.read_text(encoding="utf-8")
    assert "api_key" not in blob and "cookies" not in blob
    assert "_test-secret" not in blob and "_test-cookie" not in blob


# ---------------------------------------------------------------------------
# Regression: paused="false" does not become True
# ---------------------------------------------------------------------------

def test_paused_string_false_is_not_truthy() -> None:
    """A JSON string "false" must NOT flip paused to True. Only a real Python
    bool True counts."""
    _seed_cloud({"paused": "false"})
    assert rc.load_cloud_state()["paused"] is False
    _seed_cloud({"paused": "False"})
    assert rc.load_cloud_state()["paused"] is False
    _seed_cloud({"paused": "FALSE"})
    assert rc.load_cloud_state()["paused"] is False
    _seed_cloud({"paused": "0"})
    assert rc.load_cloud_state()["paused"] is False
    _seed_cloud({"paused": ""})
    assert rc.load_cloud_state()["paused"] is False


def test_paused_real_bool_still_works() -> None:
    _seed_cloud({"paused": True})
    assert rc.load_cloud_state()["paused"] is True
    _seed_cloud({"paused": False})
    assert rc.load_cloud_state()["paused"] is False


# ---------------------------------------------------------------------------
# Regression: existing valid cloud behavior still works
# ---------------------------------------------------------------------------

def test_normal_cloud_roundtrip() -> None:
    rc.save_cloud_state(
        update_offset=123,
        paused=True,
        working_windows=[{"name": "morning", "start": "07:00", "end": "13:00",
                          "crosses_midnight": False, "max_replies": 4}],
        drafts={"skill_prompt": "be sharp", "style_override": "analytical"},
        last_run="2026-08-23T10:00:00+00:00",
        last_scout_count=7,
        last_error_summary="boom",
    )
    state = rc.load_cloud_state()
    assert state["update_offset"] == 123
    assert state["paused"] is True
    assert state["last_scout_count"] == 7
    assert state["last_error_summary"] == "boom"
    cfg = rc.load()
    assert cfg["drafts"]["skill_prompt"] == "be sharp"
    assert cfg["drafts"]["style_override"] == "analytical"
    m = next(w for w in cfg["working_windows"] if w["name"] == "morning")
    assert m["max_replies"] == 4
    # checked-out settings.json must not have received the cloud overlay
    raw = rc.SETTINGS_PATH.read_text(encoding="utf-8")
    assert "be sharp" not in raw
    assert "13:00" not in raw


def test_corrupt_json_falls_back_safely() -> None:
    rc.CLOUD_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    rc.CLOUD_RUNTIME_PATH.write_text("not json{{{", encoding="utf-8")
    state = rc.load_cloud_state()
    assert state["update_offset"] == 0
    assert state["paused"] is False
    assert state["last_scout_count"] is None


def test_empty_file_falls_back_safely() -> None:
    rc.CLOUD_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    rc.CLOUD_RUNTIME_PATH.write_text("", encoding="utf-8")
    state = rc.load_cloud_state()
    assert state["update_offset"] == 0
    assert state["paused"] is False


if __name__ == "__main__":
    failures = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
            # Every test gets a private state directory, including tests that
            # seed raw malformed files directly.
            with runtime_isolation():
                fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            import traceback; traceback.print_exc()
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
