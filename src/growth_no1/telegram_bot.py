"""Telegram Bot Controller & Monitor for growth-no1.

Remote control is limited to the runner lifecycle (pause/resume/status/stats)
and draft review/approval. It never posts to X — approved drafts still go
through the manual paste-and-send flow (approve.py / clipboard).

Auth: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (env or config/settings.json).
Only the configured chat id may issue commands.
"""
from __future__ import annotations

import json
import logging
import os
import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

ROOT = Path(__file__).resolve().parent.parent.parent
import sys
sys_path = str(ROOT / "src" / "growth_no1")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from scheduler import TehranScheduler  # noqa: E402
from drafts import ApprovalQueue  # noqa: E402
import runtime_config  # noqa: E402

log = logging.getLogger("telegram_bot")

RUNNER_STATE = {
    "running": False,
    "paused": False,
    "last_run": None,
    "next_run": None,
    "metrics": {"evaluated": 0, "drafts": 0, "errors": 0, "day": None},
}


# ---------------------------------------------------------------- config

def _load_cfg() -> dict:
    cfg = {}
    p = ROOT / "config" / "settings.json"
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    tg = cfg.get("telegram", {})
    return {
        "token": os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token"),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id"),
    }


def _authorized(update: Update) -> bool:
    cfg = _load_cfg()
    allowed = str(cfg["chat_id"]) if cfg["chat_id"] else None
    return bool(allowed and str(update.effective_chat.id) == allowed)


# ---------------------------------------------------------------- metrics

def _bump(metric: str, n: int = 1) -> None:
    today = datetime.now().date().isoformat()
    m = RUNNER_STATE["metrics"]
    if m["day"] != today:
        m.update({"day": today, "evaluated": 0, "drafts": 0, "errors": 0})
    m[metric] = m.get(metric, 0) + n


def _queue_count() -> int:
    q = ROOT / "data" / "drafts.jsonl"
    if not q.exists():
        return 0
    rows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines() if l.strip()]
    return sum(1 for r in rows if r.get("status") == "pending")


def _status_text() -> str:
    now = datetime.now(TehranScheduler().tz)
    check = TehranScheduler().check(now)
    state = "PAUSED" if RUNNER_STATE["paused"] else (
        "Active" if check.in_window and RUNNER_STATE["running"] else "Idle/Cooldown")
    nxt = RUNNER_STATE["next_run"]
    lines = [
        f"state: {state}",
        f"tehran time: {now.strftime('%H:%M')} ({'in-window: ' + check.window_name if check.in_window else 'cooldown'})",
        f"next run: {nxt or 'n/a'}",
        f"pending drafts: {_queue_count()}",
    ]
    return "\n".join(lines)


def _stats_text() -> str:
    m = RUNNER_STATE["metrics"]
    return (f"day: {m['day']}\n"
            f"evaluated tweets: {m['evaluated']}\n"
            f"drafts generated: {m['drafts']}\n"
            f"errors: {m['errors']}")


# ---------------------------------------------------------------- handlers

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(_status_text())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(_stats_text())


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    RUNNER_STATE["paused"] = True
    await update.message.reply_text("paused. runner loop will stop after current pass.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    RUNNER_STATE["paused"] = False
    await update.message.reply_text("resumed.")


# ------------------------------------------------- live config commands

async def _reply_cfg(update: Update, cfg: dict) -> None:
    wins = "; ".join(f"{w['name']} {w['start']}-{w['end']} cap={w.get('max_replies')}"
                     for w in cfg["working_windows"])
    d = cfg["drafts"]
    await update.message.reply_text(
        f"saved.\nwindows: {wins}\nstyle: {d.get('style_override') or 'default'}\n"
        f"skill: {(d.get('skill_prompt') or 'none')[:80]}")


async def cmd_set_limit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    try:
        name, num = ctx.args[0], int(ctx.args[1])
        name = {"morning": "morning", "evening": "evening",
                "afternoon_night": "evening"}.get(name, name)
        cfg = runtime_config.set_window_limit(name, num)
        await _reply_cfg(update, cfg)
    except (IndexError, ValueError):
        await update.message.reply_text("usage: /set_limit morning|evening <number>")
    except KeyError as e:
        await update.message.reply_text(str(e))


async def cmd_set_window(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    try:
        name, start, end = ctx.args[0], ctx.args[1], ctx.args[2]
        name = {"afternoon_night": "evening"}.get(name, name)
        cfg = runtime_config.set_window_hours(name, start, end)
        await _reply_cfg(update, cfg)
    except (IndexError, ValueError):
        await update.message.reply_text(
            "usage: /set_window morning 06:00 12:00  |  /set_window evening 12:30 01:00")
    except KeyError as e:
        await update.message.reply_text(str(e))


async def cmd_set_skill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("usage: /set_skill <prompt text>  ('clear' to remove)")
        return
    cfg = runtime_config.set_skill(None if text.lower() == "clear" else text)
    await _reply_cfg(update, cfg)


STYLES = ("witty", "analytical", "supportive", "custom")


async def cmd_set_style(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    style = (ctx.args[0] if ctx.args else "").lower()
    if style not in STYLES:
        await update.message.reply_text(f"usage: /set_style {'|'.join(STYLES)}")
        return
    custom_prompt = " ".join(ctx.args[1:]).strip()
    if style == "custom" and not custom_prompt:
        await update.message.reply_text("usage: /set_style custom <prompt text>")
        return
    cfg = runtime_config.set_skill(custom_prompt or None, style)
    await _reply_cfg(update, cfg)


async def cmd_get_skill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    cfg = runtime_config.load()
    d = cfg["drafts"]
    await update.message.reply_text(
        f"style: {d.get('style_override') or '(default styles)'}\n"
        f"skill_prompt:\n{d.get('skill_prompt') or '(none)'}")


async def cmd_set_api(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "usage: /set_api <provider> <API_KEY> [endpoint_url]\n"
            "the command message is deleted after the key is saved")
        return
    provider, key = ctx.args[0], ctx.args[1]
    endpoint = ctx.args[2] if len(ctx.args) > 2 else None
    cfg = runtime_config.set_provider(provider, key, endpoint)
    try:
        await update.message.delete()  # remove key from chat history
    except Exception:
        pass
    await ctx.bot.send_message(update.effective_chat.id,
        f"provider switched to {cfg['provider']['name']}\nendpoint: {cfg['provider']['endpoint']}\n"
        f"model: {cfg['provider']['model']}\n"
        f"key: {runtime_config.mask_key(key)} (stored in gitignored config/secrets.json)")


async def cmd_current_api(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(runtime_config.masked_summary())


async def on_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    q = update.callback_query
    await q.answer()
    draft_id, action = q.data.split(":", 1)
    queue = ApprovalQueue(ROOT / "data" / "drafts.jsonl")
    if action == "skip":
        queue.set_status(draft_id, "rejected")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"skipped {draft_id}")
    else:
        queue.set_status(draft_id, "approved")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            f"approved {draft_id}\nuse approve.py / clipboard to paste + send manually")


# ---------------------------------------------------------------- outbound

def send_notification(text: str) -> bool:
    """Push a status/error notification to the admin chat (sync, thread-safe)."""
    import requests
    cfg = _load_cfg()
    if not cfg["token"] or not cfg["chat_id"]:
        log.warning("telegram not configured; dropping notification")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            json={"chat_id": cfg["chat_id"], "text": text}, timeout=15)
        return r.ok
    except Exception as e:
        log.error("send_notification failed: %s", e)
        return False


def send_candidate_review(tweet_data: dict, drafts_list: list[dict]) -> bool:
    """Send a candidate tweet + inline [Pick n]/[Skip] keyboard."""
    import requests
    cfg = _load_cfg()
    if not cfg["token"] or not cfg["chat_id"]:
        return False
    buttons = []
    for i, d in enumerate(drafts_list[:4], 1):
        buttons.append([InlineKeyboardButton(
            f"Pick {i}", callback_data=f"{d['id']}:approve")])
    buttons.append([InlineKeyboardButton("Skip",
                   callback_data=f"{drafts_list[0]['tweet_id'] if drafts_list else 'x'}:skip")])
    text = (f"@{tweet_data.get('author', '?')}: {tweet_data.get('text', '')[:280]}\n"
            + "\n".join(f"[{i}] {d['body']}" for i, d in enumerate(drafts_list, 1)))
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg['token']}/sendMessage",
            json={"chat_id": cfg["chat_id"], "text": text[:4000],
                  "reply_markup": InlineKeyboardMarkup(buttons).to_json()},
            timeout=15)
        return r.ok
    except Exception as e:
        log.error("send_candidate_review failed: %s", e)
        return False


# ---------------------------------------------------------------- bot loop

def build_app() -> Application:
    cfg = _load_cfg()
    if not cfg["token"]:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    app = Application.builder().token(cfg["token"]).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("set_limit", cmd_set_limit))
    app.add_handler(CommandHandler("set_window", cmd_set_window))
    app.add_handler(CommandHandler("set_skill", cmd_set_skill))
    app.add_handler(CommandHandler("set_style", cmd_set_style))
    app.add_handler(CommandHandler("get_skill", cmd_get_skill))
    app.add_handler(CommandHandler("set_api", cmd_set_api))
    app.add_handler(CommandHandler("current_api", cmd_current_api))
    app.add_handler(CallbackQueryHandler(on_pick))
    return app


def start_polling_background() -> threading.Thread:
    """Run the bot in a daemon thread alongside the runner loop."""
    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        RUNNER_STATE["running"] = True
        try:
            build_app().run_polling(stop_signals=None, close_loop=False)
        finally:
            RUNNER_STATE["running"] = False
    t = threading.Thread(target=_run, daemon=True, name="tg-bot")
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    RUNNER_STATE["running"] = True
    build_app().run_polling()
