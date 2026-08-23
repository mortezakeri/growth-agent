import sys
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "growth_no1"))

from scheduler import IntervalClock, TehranScheduler  # noqa: E402
from drafts import ApprovalQueue, DraftGenerator  # noqa: E402
from vision import analyze_image_bytes  # noqa: E402
import runtime_config  # noqa: E402


def test_morning_window():
    s = TehranScheduler()
    check = s.check(datetime(2026, 8, 22, 8, 0))  # 08:00 Tehran
    assert check.in_window and check.window_name == "morning"


def test_midday_gap():
    s = TehranScheduler()
    check = s.check(datetime(2026, 8, 22, 12, 10))
    assert not check.in_window


def test_afternoon_window():
    s = TehranScheduler()
    check = s.check(datetime(2026, 8, 22, 18, 0))
    assert check.in_window and check.window_name == "afternoon_night"


def test_crosses_midnight():
    s = TehranScheduler()
    assert s.check(datetime(2026, 8, 22, 23, 30)).in_window
    assert s.check(datetime(2026, 8, 22, 0, 30)).in_window
    assert not s.check(datetime(2026, 8, 22, 3, 0)).in_window  # cooldown


def test_interval_bounds():
    clock = IntervalClock(240, 720)
    for _ in range(200):
        assert 240 <= clock.next_interval() <= 720


def test_draft_queue(tmp_path):
    q = ApprovalQueue(tmp_path / "drafts.jsonl")
    gen = DraftGenerator(rng_seed=42)
    drafts = gen.generate("123", "vitalik", "web3")
    assert len(drafts) == 4
    assert q.add(drafts) == 4
    assert q.add(drafts) == 0  # dedupe
    did = drafts[0].id
    assert q.set_status(did, "approved")
    assert len(q.approved()) == 1
    assert len(q.pending()) == 3


def test_delivery_state_and_llm_drafts(tmp_path):
    q = ApprovalQueue(tmp_path / "drafts.jsonl")
    gen = DraftGenerator()
    drafts = gen.from_bodies("456", {"observant": "interesting move"})
    assert drafts[0].created_at
    q.add(drafts)
    assert not q.was_posted("456")
    assert q.record_delivery(drafts[0].id, "posted")
    assert q.was_posted("456")


def test_vision_heuristic():
    r = analyze_image_bytes("t1", b"\x89PNG fake")
    assert r.tweet_id == "t1"
    assert isinstance(r.is_web3_related, bool)


def test_runtime_config_persistence(tmp_path):
    original = runtime_config.SETTINGS_PATH
    original_secrets = runtime_config.SECRETS_PATH
    runtime_config.SETTINGS_PATH = tmp_path / "settings.json"
    runtime_config.SECRETS_PATH = tmp_path / "secrets.json"
    try:
        runtime_config.set_window_limit("morning", 7)
        runtime_config.set_window_hours("evening", "13:15", "00:45")
        runtime_config.set_skill("be precise", "analytical")
        runtime_config.set_provider("openrouter", "secret-12345678")
        cfg = runtime_config.load()
        morning = next(w for w in cfg["working_windows"] if w["name"] == "morning")
        assert morning["max_replies"] == 7
        assert cfg["drafts"]["skill_prompt"] == "be precise"
        assert cfg["provider"]["name"] == "openrouter"
        # the key must live in gitignored secrets.json, never in settings.json
        secrets = json.loads(runtime_config.SECRETS_PATH.read_text(encoding="utf-8"))
        assert secrets["openrouter"] == "secret-12345678"
        assert "secret-12345678" not in runtime_config.SETTINGS_PATH.read_text(encoding="utf-8")
        assert "secret-12345678" not in runtime_config.masked_summary()
    finally:
        runtime_config.SETTINGS_PATH = original
        runtime_config.SECRETS_PATH = original_secrets


if __name__ == "__main__":
    import tempfile
    failures = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        tmp = tempfile.mkdtemp()
        try:
            fn(Path(tmp) / "d") if fn.__code__.co_argcount else fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
