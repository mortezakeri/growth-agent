"""Interactive batch approval CLI.

Review flow per pending candidate group:
  1-4 pick a draft -> approved (copied to clipboard, compose page opened)
  e edit inline before approving
  s skip / Enter next
Human-in-the-loop invariant: this tool NEVER posts. It copies the approved
text and opens x.com/compose/post so the human pastes and sends it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drafts import ApprovalQueue  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_URL = "https://x.com/compose/post"


def copy_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["clip.exe"], input=text.encode("utf-16-le"), check=True)
        return True
    except Exception:
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            return True
        except Exception:
            return False


def edit_inline(body: str) -> str:
    print(f"current: {body}")
    new = input("new text (blank = keep): ").strip()
    return new or body


def review_group(queue: ApprovalQueue, drafts: list[dict]) -> None:
    print("\n" + "=" * 60)
    print(f"tweet {drafts[0]['tweet_id']}")
    for i, d in enumerate(drafts, 1):
        print(f"  [{i}] ({d['style']}) {d['body']}")
    while True:
        choice = input("pick [1-4], e=edit#, s=skip, q=quit: ").strip().lower()
        if choice == "s":
            return
        if choice == "q":
            sys.exit(0)
        if choice.startswith("e"):
            idx = int(choice[1:] or input("which #? ")) - 1
            drafts[idx]["body"] = edit_inline(drafts[idx]["body"])
            for i, d in enumerate(drafts, 1):
                print(f"  [{i}] ({d['style']}) {d['body']}")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(drafts):
            d = drafts[int(choice) - 1]
            queue.set_status(d["id"], "approved")
            ok = copy_to_clipboard(d["body"])
            print(f"approved: {d['body']}")
            print("clipboard copied" if ok else "copy manually ^C")
            webbrowser.open(COMPOSE_URL)
            print(f">>> paste + send manually in browser, then Enter to continue")
            input()
            return
        print("? invalid")


def main() -> int:
    ap = argparse.ArgumentParser(prog="approve")
    ap.add_argument("--queue", default=str(ROOT / "data" / "drafts.jsonl"))
    args = ap.parse_args()

    queue = ApprovalQueue(Path(args.queue))
    pending = queue.pending()
    if not pending:
        print("nothing pending.")
        return 0

    groups: dict[str, list[dict]] = {}
    for row in pending:
        groups.setdefault(row["tweet_id"], []).append(row)

    print(f"{len(pending)} pending drafts across {len(groups)} tweets")
    for tid, drafts in groups.items():
        review_group(queue, drafts)

    print(f"\ndone. approved so far: {len(queue.approved())}, "
          f"pending left: {len(queue.pending())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
