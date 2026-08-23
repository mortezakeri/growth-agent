"""Tehran-timezone working-window scheduler with polite randomized intervals.

Windows (Asia/Tehran):
  - morning:        06:00 - 12:00
  - afternoon_night 12:30 - 01:00 (next day)

Read sessions are spaced by a uniform random interval of 4-12 minutes.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")

DEFAULT_WINDOWS = [
    ("morning", time(6, 0), time(12, 0), False),
    ("afternoon_night", time(12, 30), time(1, 0), True),
]


@dataclass
class WindowCheck:
    in_window: bool
    window_name: str | None = None
    next_open_utc: datetime | None = None
    minutes_until_close: float | None = None


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class TehranScheduler:
    def __init__(
        self,
        windows: list[tuple[str, str, str]] | None = None,
        tz: ZoneInfo = TEHRAN,
    ) -> None:
        """windows: list of (name, start HH:MM, end HH:MM). End < start => crosses midnight."""
        self.tz = tz
        self.windows = []
        for name, start, end in windows or []:
            s, e = _parse_hhmm(start), _parse_hhmm(end)
            self.windows.append((name, s, e, e < s))
        if not self.windows:
            self.windows = list(DEFAULT_WINDOWS)

    def check(self, now: datetime | None = None) -> WindowCheck:
        if now is None:
            now = datetime.now(self.tz)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=self.tz)  # naive input == Tehran wall clock
        else:
            now = now.astimezone(self.tz)
        t = now.time()

        best_next_open: datetime | None = None
        for name, start, end, crosses in self.windows:
            if crosses:
                inside = t >= start or t <= end
                close_dt = (now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
                            + (timedelta(days=1) if t > end else timedelta(0)))
                open_dt = (now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
                           + (timedelta(days=1) if t >= start else timedelta(0)))
                if not inside:
                    open_dt = (now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
                               + (timedelta(days=1) if start <= t else timedelta(0)))
            else:
                inside = start <= t < end
                close_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
                open_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
                if not inside and start < t:
                    open_dt += timedelta(days=1)

            if not inside and t < end and crosses is False and start > t:
                pass  # open today, already computed

            if inside:
                return WindowCheck(True, name, None, (close_dt - now).total_seconds() / 60.0)
            if best_next_open is None or open_dt < best_next_open:
                best_next_open = open_dt

        return WindowCheck(False, None, best_next_open, None)


@dataclass
class IntervalClock:
    """Polite read-interval clock: uniform random between min and max seconds."""
    min_s: int = 240
    max_s: int = 720
    rng: random.Random = field(default_factory=random.Random)

    def next_interval(self) -> float:
        return self.rng.uniform(self.min_s, self.max_s)

    def next_run_after(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(tz=TEHRAN)
        return now + timedelta(seconds=self.next_interval())
