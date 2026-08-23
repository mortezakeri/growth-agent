"""Persistent per-Tehran-day reply counters for dynamic shift caps."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent.parent
PATH = ROOT / "data" / "shift_usage.json"
_LOCK = threading.Lock()
TEHRAN = ZoneInfo("Asia/Tehran")


def _today() -> str:
    return datetime.now(TEHRAN).date().isoformat()


def _read() -> dict:
    if not PATH.exists():
        return {"day": _today(), "counts": {}}
    data = json.loads(PATH.read_text(encoding="utf-8"))
    return data if data.get("day") == _today() else {"day": _today(), "counts": {}}


def used(shift: str) -> int:
    with _LOCK:
        return int(_read()["counts"].get(shift, 0))


def increment(shift: str, amount: int = 1) -> int:
    with _LOCK:
        data = _read()
        data["counts"][shift] = int(data["counts"].get(shift, 0)) + amount
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(PATH)
        return data["counts"][shift]
