# growth-no1

Standalone Web3/GM reply agent for X. It scouts, scores, drafts, and can deliver
replies through Playwright. Delivery starts in dry-run mode.

## Layout

```
src/growth_no1/
  scheduler.py   Tehran-timezone working windows + 4-12min polite interval clock
  scout.py       Cookie-auth Playwright reader and scorer
  reply_agent.py Cookie-auth Playwright reply delivery
  vision.py      Multimodal image analysis hook (provider-injectable)
  drafts.py      Draft generator (4 styles) + JSONL approval queue
  runner.py      --once / --loop entry point
config/settings.json   windows, intervals, keywords, draft rules
data/                  candidates.json, drafts.jsonl, cookies.json (gitignored!)
```

## Working windows (Asia/Tehran)

| shift | window |
|---|---|
| morning | 06:00 – 12:00 |
| afternoon/night | 12:30 – 01:00 (+1) |
| silent | everything else |

## Usage

```bash
cd src/growth_no1
python runner.py --once          # one scout pass now (no window check)
python runner.py --loop          # continuous, window-aware, 4-12 min jitter
```

Cookies go in `data/cookies.json` (`auth_token`, `ct0`) — never committed.
Approve drafts by editing `data/drafts.jsonl` status fields or via
`ApprovalQueue.batch_approve([...])` in Python.

## Reply mode

`config/settings.json` enables the reply pipeline with `dry_run: true`. In this
mode Playwright opens the tweet and fills the reply, but does not click Send.
After verifying login and selectors, set `dry_run` to `false`. The runner posts
at most `max_replies_per_pass`, records `posted/failed/dry_run`, and never posts
twice to a tweet already recorded as posted. Credentials stay out of Git/logs.

## GitHub Actions

The workflow at `.github/workflows/growth-agent.yml` runs every 10 minutes and
executes only during the configured Tehran windows. Add these repository
secrets under **Settings → Secrets and variables → Actions**:

- `X_AUTH_TOKEN`
- `X_CT0`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `NOUS_API_KEY` (optional; local templates are used without it)

Scheduled runs are dry-run by default. After one successful manual dry-run,
create the repository variable `LIVE_REPLIES=true` to let scheduled runs click
Send. Manual workflow runs always expose their own `dry_run` checkbox. Reply
history is carried between Actions runs using a private Actions cache.

## Live Telegram control

Run `python src/growth_no1/runner.py --loop` for the persistent process. It
starts the authenticated Telegram controller beside the runner and reloads
`config/settings.json` every cycle. Available commands:

- `/set_limit morning|evening NUMBER`
- `/set_window morning|evening HH:MM HH:MM`
- `/set_skill PROMPT`, `/set_style witty|analytical|supportive|custom [PROMPT]`
- `/get_skill`
- `/set_api PROVIDER API_KEY [ENDPOINT]`, `/current_api`
- `/status`, `/stats`, `/pause`, `/resume`

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the process environment.
Only that chat ID is accepted. `/set_api` deletes the command message when
possible and all API displays are masked. Because the requested API key is
persisted in `config/settings.json`, never commit that file after setting a
live key; use GitHub Actions Secrets for hosted runs.
