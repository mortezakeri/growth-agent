"""Entry point: --once for a single scout pass, --loop for window-aware continuous runs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src" / "growth_no1"))

from scheduler import TehranScheduler  # noqa: E402
from scout import ScoutEngine, save_candidates  # noqa: E402
from drafts import ApprovalQueue, Draft, DraftGenerator  # noqa: E402
from reply_agent import ReplyAgent  # noqa: E402
from shift_quota import increment as increment_quota, used as quota_used  # noqa: E402
import nous_client  # noqa: E402
import cookies as cookie_store  # noqa: E402
from scout import evaluate_candidate, DEFAULT_BLOCKED_TERMS  # noqa: E402


def load_settings() -> dict:
    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def load_cookies() -> list[dict]:
    """Cookie Editor V3 export from X_COOKIES_JSON, or data/cookies.json fallback."""
    try:
        return cookie_store.load_cookie_source()
    except cookie_store.CookieError as e:
        print(f"cookie error: {e}")
        return []


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def delivery_allowed(draft_source: str, dry_run: bool) -> bool:
    """Local templates may exercise dry-run UI but can never be sent live."""
    return dry_run or draft_source == "llm"


def scheduled_decision() -> tuple[bool, str, dict, object | None]:
    """Cloud-aware preflight shared by the workflow and --scheduled."""
    import runtime_config
    cloud = runtime_config.load_cloud_state()
    cfg = runtime_config.load()
    if cloud["paused"]:
        return False, "paused", cfg, None
    sched = TehranScheduler([(w["name"], w["start"], w["end"])
                             for w in cfg["working_windows"]])
    check = sched.check()
    if not check.in_window:
        return False, "outside_window", cfg, check
    return True, "in_window", cfg, check


def notify_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    import requests
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]}, timeout=15,
        )
        return response.ok
    except Exception as exc:
        print(f"telegram notification failed: {exc}")
        return False


def run_pass(cfg: dict, window_name: str | None = None) -> dict[str, int]:
    cookies = load_cookies()
    if not cookies:
        return {"scouted": 0, "drafts": 0, "replies": 0, "failed": 1}
    engine = ScoutEngine(
        cookies=cookies,
        keywords=cfg["scout"]["keywords"],
        min_interval_s=cfg["read_interval_seconds"]["min"],
        max_interval_s=cfg["read_interval_seconds"]["max"],
    )
    queue = ApprovalQueue(ROOT / "data" / "drafts.jsonl")
    gen = DraftGenerator()
    provider = cfg.get("provider", {})
    try:
        import runtime_config
        _key, _src = runtime_config.get_active_api_key()
        use_llm = bool(_key)
    except Exception:
        use_llm = bool(provider.get("api_key") or
                       os.environ.get(provider.get("api_key_env", ""), ""))
    query = " OR ".join(cfg["scout"]["keywords"][:4])
    tweets = engine.search(query, limit=cfg["scout"]["max_tweets_per_session"])
    save_candidates(tweets, ROOT / "data" / "candidates.json")

    # multimodal pass over images when the provider is wired
    from vision import analyze_image_url, save_analyses
    analyses = []
    for t in tweets:
        if t.has_image and t.image_urls and use_llm:
            try:
                analyses.append(analyze_image_url(t.id, t.image_urls[0]))
            except Exception as e:
                print(f"[vision] {t.id}: {e}")
    if analyses:
        save_analyses(analyses, ROOT / "data" / "image_analyses.json")

    added = 0
    reply_cfg = cfg.get("reply", {})
    replier = None
    if reply_cfg.get("enabled"):
        replier = ReplyAgent(
            cookies=cookies,
            headless=reply_cfg.get("headless", True),
            dry_run=_env_bool("REPLY_DRY_RUN", reply_cfg.get("dry_run", True)),
        )
    sent = 0
    reply_attempts = 0
    failed = 0
    max_replies = int(reply_cfg.get("max_replies_per_pass", 1))
    if window_name:
        window = next((w for w in cfg["working_windows"] if w["name"] == window_name), {})
        remaining = max(0, int(window.get("max_replies", max_replies)) - quota_used(window_name))
        max_replies = min(max_replies, remaining)
    preferred_style = reply_cfg.get("style", "observant")
    min_matches = int(cfg["scout"].get("min_keyword_matches", 1))
    configured_blocked_terms = cfg["scout"].get("blocked_terms")
    blocked_terms = (DEFAULT_BLOCKED_TERMS if configured_blocked_terms is None
                     else configured_blocked_terms)
    skip_log = []
    for t in tweets[: cfg["drafts"]["batch_size"]]:
        gate = evaluate_candidate(t.text, cfg["scout"]["keywords"],
                                  min_matches=min_matches,
                                  blocked_terms=blocked_terms)
        if not gate["approved"]:
            skip_log.append({"id": t.id, "reason": gate["blocked_reason"]})
            continue
        if use_llm:
            bodies = nous_client.generate_drafts(t.author, t.text, gen.styles)
            drafts = gen.from_bodies(t.id, bodies)
            draft_source = "llm"
        else:
            drafts = gen.generate(t.id, t.author or "friend", topic="web3")
            draft_source = "local_fallback"
        added += queue.add(drafts)
        if replier and reply_attempts < max_replies and not queue.was_posted(t.id):
            chosen = next((d for d in drafts if d.style == preferred_style), drafts[0])
            # This gate MUST precede ReplyAgent.reply(): in live mode that
            # method clicks Send before returning status="posted".
            if not delivery_allowed(draft_source, replier.dry_run):
                failed += 1
                print(f"BLOCKED live delivery from {draft_source} draft; tweet {t.id}")
                continue
            result = replier.reply(
                t.url, chosen.body,
                report_extra={
                    "candidate_tweet_text": t.text[:280],
                    "matched_keywords": gate["matched_keywords"],
                    "relevance_approved": True,
                    "blocked_reason": None,
                    "draft_source": draft_source,
                    "canonical_tweet_id": t.id,
                    "canonical_tweet_url": t.url,
                })
            reply_attempts += 1
            queue.record_delivery(chosen.id, result.status, result.error)
            if result.status == "posted":
                sent += 1
                if window_name:
                    increment_quota(window_name)
                notify_telegram(f"reply posted\n{t.url}\n\n{chosen.body}")
            elif result.status == "failed":
                failed += 1
                notify_telegram(f"reply failed\n{t.url}\n\n{result.error or 'unknown error'}")
    if skip_log:
        (ROOT / "data" / "skip_log.json").write_text(
            json.dumps(skip_log, indent=2)[:100_000], encoding="utf-8")
        print(f"skipped_candidates={len(skip_log)} (see data/skip_log.json)")
    print(f"scouted={len(tweets)} analyzed={len(analyses)} "
          f"new_drafts={added} replies={sent}")
    return {"scouted": len(tweets), "drafts": added, "replies": sent, "failed": failed}


pass_count = [1]


def main() -> int:
    ap = argparse.ArgumentParser(prog="growth-no1")
    ap.add_argument("--once", action="store_true", help="single pass, ignore windows")
    ap.add_argument("--scheduled", action="store_true", help="single pass only inside a working window")
    ap.add_argument("--loop", action="store_true", help="continuous, window-aware")
    ap.add_argument("--scheduled-check", action="store_true",
                    help="print GitHub output deciding whether browser work is needed")
    args = ap.parse_args()
    cfg = load_settings()

    if args.scheduled_check:
        should_run, reason, _cfg, _check = scheduled_decision()
        print(f"should_run={'true' if should_run else 'false'}")
        print(f"reason={reason}")
        return 0

    if args.once:
        check = TehranScheduler([(w["name"], w["start"], w["end"])
                                 for w in cfg["working_windows"]]).check()
        stats = run_pass(cfg, check.window_name)
        notify_telegram("growth-no1 run complete\n" + "\n".join(
            f"{key}: {value}" for key, value in stats.items()))
        return 0

    if args.scheduled:
        import runtime_config
        should_run, reason, cfg, check = scheduled_decision()
        if not should_run:
            print(f"scheduled pass skipped: {reason}")
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            stats = run_pass(cfg, check.window_name)
        except Exception as exc:
            runtime_config.save_cloud_state(last_run=now,
                                            last_error_summary=type(exc).__name__)
            raise
        runtime_config.save_cloud_state(last_run=now,
                                        last_successful_run=now,
                                        last_scout_count=stats.get("scouted", 0),
                                        last_error_summary="")
        notify_telegram("growth-no1 scheduled run complete\n" + "\n".join(
            f"{key}: {value}" for key, value in stats.items()))
        return 0

    if not args.loop:
        ap.print_help()
        return 1

    try:
        import telegram_bot
        telegram_bot.start_polling_background()
    except Exception as exc:
        print(f"telegram control not started: {exc}")

    import time as _time
    while True:
        cfg = load_settings()  # Telegram changes take effect without restart.
        sched = TehranScheduler([(w["name"], w["start"], w["end"])
                                 for w in cfg["working_windows"]])
        from scheduler import IntervalClock
        clock = IntervalClock(cfg["read_interval_seconds"]["min"],
                              cfg["read_interval_seconds"]["max"])
        check = sched.check()
        if check.in_window:
            try:
                import telegram_bot
                if not telegram_bot.RUNNER_STATE["paused"]:
                    telegram_bot.RUNNER_STATE["last_run"] = datetime.now(timezone.utc).isoformat()
                    run_pass(cfg, check.window_name)
            except Exception as e:
                print(f"pass error: {e}")
            nxt = clock.next_interval()
        else:
            nxt = max(60.0, ((check.next_open_utc.astimezone(timezone.utc)
                              - datetime.now(timezone.utc)).total_seconds()
                             if check.next_open_utc else 300))
            print(f"outside working window; sleeping {nxt/60:.1f} min")
        # Short chunks let pause/window/limit changes become effective promptly.
        remaining = nxt
        while remaining > 0:
            chunk = min(30.0, remaining)
            _time.sleep(chunk)
            remaining -= chunk


if __name__ == "__main__":
    raise SystemExit(main())
