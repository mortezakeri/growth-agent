"""Draft generator + batch approval queue.

Human-in-the-loop invariant: drafts are produced locally for manual review;
nothing here posts to X.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Draft:
    id: str
    tweet_id: str
    style: str
    body: str
    created_at: str
    status: str = "pending"  # pending | approved | rejected


STYLES = ("gm", "observant", "curious", "dry_humor")

_TEMPLATES = {
    "gm": ["gm {author}", "gm {author} gm", "gm {author}. gm to everyone building"],
    "observant": ["{topic} moving faster than most notice", "{topic} keeps showing up in my feed today"],
    "curious": ["what made you pick {topic}, {author}?", "genuinely curious how {topic} plays out"],
    "dry_humor": ["{topic} again. bold strategy", "another day another {topic}. fine"],
}


class DraftGenerator:
    def __init__(self, styles: tuple[str, ...] = STYLES, rng_seed: int | None = None):
        import random
        self.styles = styles
        self._rng = random.Random(rng_seed)

    def generate(self, tweet_id: str, author: str, topic: str) -> list[Draft]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        drafts = []
        for style in self.styles:
            template = self._rng.choice(_TEMPLATES[style])
            body = template.format(author=author, topic=topic).strip()
            drafts.append(Draft(
                id=f"{tweet_id}-{style}",
                tweet_id=tweet_id,
                style=style,
                body=body.lower().rstrip("."),
                created_at=now,
            ))
        return drafts

    def from_bodies(self, tweet_id: str, bodies: dict[str, str]) -> list[Draft]:
        """Build valid timestamped Draft objects from LLM output."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [Draft(f"{tweet_id}-{style}", tweet_id, style, body, now)
                for style, body in bodies.items()]


class ApprovalQueue:
    """JSONL-backed queue. approve/reject only mutate local state."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def add(self, drafts: list[Draft]) -> int:
        existing_ids = {d["id"] for d in self._read()}
        new = [asdict(d) for d in drafts if d.id not in existing_ids]
        with self.path.open("a", encoding="utf-8") as f:
            for row in new:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(new)

    def _read(self) -> list[dict]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(l) for l in lines if l.strip()]

    def set_status(self, draft_id: str, status: str) -> bool:
        assert status in ("pending", "approved", "rejected", "posted", "failed", "dry_run")
        rows = self._read()
        hit = False
        for r in rows:
            if r["id"] == draft_id:
                r["status"] = status
                hit = True
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)
        return hit

    def record_delivery(self, draft_id: str, status: str, error: str | None = None) -> bool:
        rows = self._read()
        hit = False
        for row in rows:
            if row["id"] == draft_id:
                row["status"] = status
                row["delivery_error"] = error
                row["delivered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                hit = True
        if hit:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                           encoding="utf-8")
            tmp.replace(self.path)
        return hit

    def was_posted(self, tweet_id: str) -> bool:
        return any(r.get("tweet_id") == tweet_id and r.get("status") == "posted"
                   for r in self._read())

    def batch_approve(self, prefix_or_ids: list[str]) -> int:
        n = 0
        for did in prefix_or_ids:
            if self.set_status(did, "approved"):
                n += 1
        return n

    def approved(self) -> list[dict]:
        return [r for r in self._read() if r["status"] == "approved"]

    def pending(self) -> list[dict]:
        return [r for r in self._read() if r["status"] == "pending"]
