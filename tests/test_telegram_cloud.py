"""Tests for one-shot Telegram cloud mode (telegram_once + overlay + runner)."""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

import runtime_config as rc  # noqa: E402
import telegram_once as tgo  # noqa: E402


@contextmanager
def cloud_isolation():
    """Full isolation: paths, env vars, telegram_once monkeypatches.

    Everything restored on exit, even on assertion failure.
    """
    sandbox = Path(tempfile.mkdtemp())
    (sandbox / "config").mkdir()
    (sandbox / "data").mkdir()
    saved = {
        "settings": rc.SETTINGS_PATH,
        "cloud": rc.CLOUD_RUNTIME_PATH,
        "secrets": rc.SECRETS_PATH,
        "save_cloud_state": rc.save_cloud_state,
        "tg": tgo._tg,
        "reply": tgo._reply,
        "env": {k: os.environ.get(k) for k in
                ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")},
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
    os.environ.update({"TELEGRAM_BOT_TOKEN": "test-token",
                       "TELEGRAM_CHAT_ID": "111"})

    # default stubs; individual tests may override within the block
    sent: list[tuple[str, str]] = []
    updates: dict = {"ok": True, "result": []}
    tg_calls: list[tuple[str, dict]] = []

    def fake_tg(token, method, payload):
        tg_calls.append((method, payload))
        if method == "getUpdates":
            return updates
        return {"ok": True}

    tgo._tg = fake_tg
    tgo._reply = lambda token, chat, text: sent.append((chat, text))

    ctx = SimpleNamespace(sent=sent, tg_calls=tg_calls, updates=updates)
    ctx.set_updates = lambda result: updates.__setitem__("result", result)

    try:
        yield ctx
    finally:
        rc.SETTINGS_PATH = saved["settings"]
        rc.CLOUD_RUNTIME_PATH = saved["cloud"]
        rc.SECRETS_PATH = saved["secrets"]
        rc.save_cloud_state = saved["save_cloud_state"]
        tgo._tg = saved["tg"]
        tgo._reply = saved["reply"]
        for k, v in saved["env"].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _update(uid, text, chat_id="111"):
    return {"update_id": uid, "message": {"message_id": uid,
            "chat": {"id": int(chat_id)}, "text": text}}


def test_authorized_vs_unauthorized():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([_update(5, "/status", chat_id="999")])
        n = tgo.process_once()
        assert n == 0 and not ctx.sent, "unauthorized chat must get no reply"
        assert rc.load_cloud_state()["update_offset"] == 6, \
            "offset must advance even for skipped chats"
        ctx.set_updates([_update(6, "/status", chat_id="111")])
        n = tgo.process_once()
        assert n == 1
        assert ctx.sent and ctx.sent[0][0] == "111" and "cloud" in ctx.sent[0][1]
        assert any(method == "setMyCommands" for method, _ in ctx.tg_calls)


def test_dedup_and_offset_persistence():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        # same update delivered twice (Telegram redelivery)
        ctx.set_updates([_update(10, "/pause"), _update(10, "/pause")])
        tgo.process_once()
        state = rc.load_cloud_state()
        assert state["update_offset"] == 11, "offset = max_id + 1"
        assert state["paused"] is True
        assert len(ctx.sent) == 1, "duplicate update must be handled once per batch"
        # re-run with empty result: nothing reprocessed, getUpdates used saved offset
        ctx.set_updates([])
        ctx.sent.clear()
        tgo.process_once()
        assert not ctx.sent, "same update must not be processed twice"
        method, payload = ctx.tg_calls[-1]
        assert method == "getUpdates" and payload["offset"] == 11
        assert payload["timeout"] == 0


def test_window_limit_persistence():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([
            _update(20, "/set_limit morning 3"),
            _update(21, "/set_window evening 21:00 02:30"),
        ])
        tgo.process_once()
        cfg = rc.load()  # overlay must be visible through load()
        m = next(w for w in cfg["working_windows"] if w["name"] == "morning")
        e = next(w for w in cfg["working_windows"] if w["name"] == "evening")
        assert m["max_replies"] == 3
        assert e["start"] == "21:00" and e["crosses_midnight"] is True
        # checked-out settings.json untouched
        assert "21:00" not in rc.SETTINGS_PATH.read_text(encoding="utf-8")


def test_skill_style_persistence():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([
            _update(30, "/set_skill focus on defi infra"),
            _update(31, "/set_style analytical"),
            _update(32, "/set_style custom be brief"),
        ])
        tgo.process_once()
        cfg = rc.load()
        assert cfg["drafts"]["skill_prompt"] == "be brief", "custom overwrites skill"
        assert cfg["drafts"]["style_override"] == "custom"


def test_malformed_command_gets_usage():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([
            _update(40, "/set_limit morning"),          # missing number
            _update(41, "/set_limit morning abc"),      # non-number
            _update(42, "/set_window morning 99:99 10:00"),
            _update(43, "/set_style nonsense"),
        ])
        n = tgo.process_once()  # must not crash
        assert n == 4
        assert len(ctx.sent) == 4
        assert all("usage" in r or "invalid" in r for _, r in ctx.sent)
        assert rc.load_cloud_state()["update_offset"] == 44


def test_set_api_rejected_in_cloud():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([_update(50, "/set_api openrouter sk-super-secret-key-123")])
        tgo.process_once()
        assert ctx.sent, "must reply"
        reply = ctx.sent[0][1]
        assert "GitHub Repository Secrets" in reply
        assert "sk-super-secret-key-123" not in reply
        deletes = [payload for method, payload in ctx.tg_calls
                   if method == "deleteMessage"]
        assert deletes and deletes[0]["message_id"] == 50, \
            "message containing an API key must be deleted"
        # key must never be persisted anywhere
        if rc.CLOUD_RUNTIME_PATH.exists():
            assert "sk-super-secret-key-123" not in rc.CLOUD_RUNTIME_PATH.read_text(encoding="utf-8")
        assert not rc.SECRETS_PATH.exists(), "no secrets file may be created"


def test_no_secrets_in_cloud_runtime():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=99, paused=True,
                            working_windows=[{"name": "morning", "start": "06:00",
                                              "end": "12:00", "max_replies": 2}],
                            drafts={"skill_prompt": "x", "style_override": "witty"},
                            # attempted secret injection must be dropped:
                            bot_token="secret-token", cookies={"auth_token": "s"},
                            api_key="sk-xyz", authorization="Bearer z")
        blob = rc.CLOUD_RUNTIME_PATH.read_text(encoding="utf-8")
        for secret in ("secret-token", "sk-xyz", "Bearer z", '"auth_token"'):
            assert secret not in blob, secret
        state = json.loads(blob)
        assert state["paused"] is True and state["update_offset"] == 99


def test_non_message_and_unknown_command_are_safe():
    with cloud_isolation() as ctx:
        rc.save_cloud_state(update_offset=0)
        ctx.set_updates([
            {"update_id": 70, "callback_query": {"data": "ignored"}},
            _update(71, "/does_not_exist"),
        ])
        assert tgo.process_once() == 1
        assert len(ctx.sent) == 1 and "/help" in ctx.sent[0][1]
        assert rc.load_cloud_state()["update_offset"] == 72


def test_runner_honors_pause_and_overlay():
    with cloud_isolation():
        rc.save_cloud_state(paused=True, working_windows=[
            {"name": "morning", "start": "06:00", "end": "12:00", "max_replies": 2},
            {"name": "evening", "start": "12:30", "end": "01:00",
             "crosses_midnight": True, "max_replies": 7}])
        import runner
        should_run, reason, cfg, _check = runner.scheduled_decision()
        assert should_run is False and reason == "paused"
        e = next(w for w in cfg["working_windows"] if w["name"] == "evening")
        assert e["max_replies"] == 7, "overlay windows must be visible to runner"


def test_cloud_session_delay_bounds():
    import random
    import runner
    initial, later = runner._session_delays(
        {"read_interval_seconds": {"min": 240, "max": 720}}, random.Random(7))
    assert 0 <= initial <= 120
    assert all(240 <= later() <= 720 for _ in range(20))


def test_overlay_load_merges_safely():
    with cloud_isolation():
        rc.save_cloud_state(drafts={"skill_prompt": "s", "style_override": "witty",
                                    "evil_key": "nope"})
        cfg = rc.load()
        assert cfg["drafts"]["skill_prompt"] == "s"
        assert "evil_key" not in cfg["drafts"]


if __name__ == "__main__":
    failures = 0
    tests = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    for name, fn in tests.items():
        try:
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
