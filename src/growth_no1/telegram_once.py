"""One-shot Telegram command processor for GitHub Actions.

Polls getUpdates exactly once (timeout=0), processes pending commands for the
authorized chat only, replies, and advances the stored offset. Designed to run
inside a short scheduled workflow — no long polling, no VPS.

Mutations write to the safe cloud overlay (data/cloud_runtime.json), never to
the checked-out config/settings.json. API keys are never accepted here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_config as rc  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"

USAGE = (
    "commands:\n"
    "/status — agent state\n"
    "/stats — daily metrics\n"
    "/pause | /resume\n"
    "/set_limit morning|evening <number>\n"
    "/set_window morning|evening HH:MM HH:MM\n"
    "/set_skill <prompt> ('clear' to remove)\n"
    "/set_style witty|analytical|supportive|custom [prompt]\n"
    "/get_skill\n"
    "/current_api\n"
    "/help"
)


def _tg(token: str, method: str, payload: dict) -> dict | None:
    """One Telegram call; errors are reported without exposing the token."""
    try:
        r = requests.post(API.format(token=token, method=method),
                          json=payload, timeout=20)
        if not r.ok:
            body = r.json() if "json" in r.headers.get("content-type", "") else {}
            desc = str(body.get("description", f"HTTP {r.status_code}"))[:200]
            print(f"telegram {method} failed: {desc}")
            return None
        return r.json()
    except requests.RequestException as e:
        print(f"telegram {method} network error: {type(e).__name__}")
        return None


def _reply(token: str, chat_id: str, text: str) -> None:
    _tg(token, "sendMessage", {"chat_id": chat_id, "text": text[:4000]})


def _windows_from_overlay(cfg: dict) -> list[dict]:
    return cfg["working_windows"]


def _status_text() -> str:
    from datetime import datetime
    from scheduler import TehranScheduler
    cfg = rc.load()                       # settings + cloud overlay
    cloud = rc.load_cloud_state()
    sched = TehranScheduler([(w["name"], w["start"], w["end"])
                             for w in _windows_from_overlay(cfg)])
    check = sched.check()
    now = datetime.now(sched.tz)
    state = "paused" if cloud["paused"] else (
        "running (in window)" if check.in_window else "idle/cooldown")
    wins = "; ".join(f"{w['name']} {w['start']}-{w['end']} "
                     f"cap={w.get('max_replies', '?')}"
                     for w in _windows_from_overlay(cfg))
    d = cfg["drafts"]
    provider = cfg.get("provider", {})
    key, _source = rc.get_active_api_key()
    return (
        "mode: cloud (GitHub Actions)\n"
        f"state: {state}\n"
        f"tehran time: {now.strftime('%H:%M')}"
        f" ({'in-window: ' + check.window_name if check.in_window else 'cooldown'})\n"
        f"windows: {wins}\n"
        f"style: {d.get('style_override') or '(default)'}\n"
        f"skill: {(d.get('skill_prompt') or '(none)')[:100]}\n"
        f"provider: {provider.get('name', 'none')}\n"
        f"api key: {'set' if key else 'not set'}\n"
        f"last scheduled run: {cloud.get('last_run') or 'never'}\n"
        f"last scout count: {cloud.get('last_scout_count') if cloud.get('last_scout_count') is not None else 'n/a'}"
    )


def _handle_command(token: str, chat_id: str, text: str) -> str | None:
    """Returns reply text. All mutations go to the cloud overlay only."""
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    args = parts[1:]

    if cmd == "/status":
        return _status_text()
    if cmd == "/stats":
        cloud = rc.load_cloud_state()
        count = cloud.get("last_scout_count")
        return (f"last scheduled run: {cloud.get('last_run') or 'never'}\n"
                f"last scout count: {count if count is not None else 'n/a'}")
    if cmd == "/pause":
        rc.save_cloud_state(paused=True)
        return "paused. next scheduled pass will skip (takes effect next run, <=10 min)."
    if cmd == "/resume":
        rc.save_cloud_state(paused=False)
        return "resumed."
    if cmd == "/set_limit":
        try:
            name, num = args[0], int(args[1])
            if not 0 <= num <= 100:
                raise ValueError
        except (IndexError, ValueError):
            return "usage: /set_limit morning|evening <number 0-100>"
        name = {"morning": "morning", "evening": "evening",
                "afternoon_night": "evening"}.get(name, name)
        cfg = rc.load()
        wins = _windows_from_overlay(cfg)
        hit = False
        for w in wins:
            if w["name"] == name:
                w["max_replies"] = num
                hit = True
        if not hit:
            return f"unknown window '{name}'"
        rc.save_cloud_state(working_windows=wins)
        return f"saved: {name} cap={num} (effective next run)"
    if cmd == "/set_window":
        if len(args) != 3:
            return "usage: /set_window morning|evening HH:MM HH:MM"
        name, start, end = args
        def parse(s):
            h, m = s.split(":")
            h, m = int(h), int(m)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return f"{h:02d}:{m:02d}"
        try:
            start, end = parse(start), parse(end)
        except (ValueError, IndexError):
            return "invalid time; use HH:MM"
        name = {"afternoon_night": "evening"}.get(name, name)
        cfg = rc.load()
        wins = _windows_from_overlay(cfg)
        hit = False
        for w in wins:
            if w["name"] == name:
                w["start"], w["end"] = start, end
                w["crosses_midnight"] = end < start
                hit = True
        if not hit:
            return f"unknown window '{name}'"
        rc.save_cloud_state(working_windows=wins)
        return f"saved: {name} {start}-{end} (effective next run)"
    if cmd == "/set_skill":
        if not args:
            return "usage: /set_skill <prompt> ('clear' to remove)"
        prompt = " ".join(args)
        value = None if prompt.lower() == "clear" else prompt[:500]
        cfg = rc.load()
        drafts = dict(cfg["drafts"]) if isinstance(cfg.get("drafts"), dict) else {}
        drafts["skill_prompt"] = value
        rc.save_cloud_state(drafts=drafts)
        return "skill saved (effective next run)" if value else "skill cleared"
    if cmd == "/set_style":
        style = args[0].lower() if args else ""
        if style not in ("witty", "analytical", "supportive", "custom"):
            return "usage: /set_style witty|analytical|supportive|custom [prompt]"
        cfg = rc.load()
        drafts = dict(cfg["drafts"]) if isinstance(cfg.get("drafts"), dict) else {}
        drafts["style_override"] = style
        if style == "custom":
            if len(args) < 2:
                return "custom style needs a prompt: /set_style custom <prompt>"
            drafts["skill_prompt"] = " ".join(args[1:])[:500]
        rc.save_cloud_state(drafts=drafts)
        return f"style set to {style} (effective next run)"
    if cmd == "/get_skill":
        d = rc.load()["drafts"]
        skill = d.get("skill_prompt") or "(none)"
        # safe formatting: strip markdown-ish chars from user text
        skill = skill.replace("`", "'")
        return f"style: {d.get('style_override') or '(default)'}\nskill: {skill}"
    if cmd == "/current_api":
        return ("cloud mode: provider keys come from GitHub Repository Secrets.\n"
                + rc.masked_summary())
    if cmd == "/set_api":
        return "For GitHub Actions, configure the provider key using GitHub Repository Secrets."
    if cmd == "/help":
        return USAGE
    return "unknown command. use /help"


def process_once() -> int:
    """Returns number of updates processed."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("telegram not configured; skipping command processing")
        return 0

    offset = rc.load_cloud_state()["update_offset"]
    data = _tg(token, "getUpdates",
               {"offset": offset, "timeout": 0, "limit": 100})
    if not data or not data.get("ok"):
        return 0

    processed = 0
    max_id = offset
    seen_ids: set[int] = set()
    for update in sorted(data.get("result", []), key=lambda u: u.get("update_id", 0)):
        uid = update.get("update_id")
        if uid is None or uid < offset or uid in seen_ids:
            continue  # dedupe: already handled
        seen_ids.add(uid)
        msg = update.get("message") or update.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            max_id = max(max_id, uid + 1)
            continue  # unauthorized chat: skip silently, still advance offset
        text = msg.get("text", "").strip()
        if text.startswith("/"):
            if text.lower().split(maxsplit=1)[0].split("@")[0] == "/set_api":
                message_id = msg.get("message_id")
                if message_id is not None:
                    _tg(token, "deleteMessage",
                        {"chat_id": chat_id, "message_id": message_id})
            reply = _handle_command(token, chat_id, text)
            if reply:
                _reply(token, chat_id, reply)
        max_id = max(max_id, uid + 1)
        processed += 1

    if max_id > offset:
        rc.save_cloud_state(update_offset=max_id)
    return processed


if __name__ == "__main__":
    n = process_once()
    print(f"telegram_once: processed {n} update(s)")
