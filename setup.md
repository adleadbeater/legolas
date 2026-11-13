# Legolas v2 — Setup

## 1. Python dependencies

```bash
pip3 install -r requirements.txt
```

## 2. Credentials

Add these to `~/.claude/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
SLACK_WEBHOOK=https://hooks.slack.com/services/...
SLACK_BOT_TOKEN=xoxb-...
```

- **ANTHROPIC_API_KEY** — from console.anthropic.com → API Keys
- **SLACK_WEBHOOK** — from api.slack.com → Your Apps → Incoming Webhooks (used for posting cards)
- **SLACK_BOT_TOKEN** — from api.slack.com → Your Apps → OAuth & Permissions (used for reading reactions)

## 3. Google Sheets auth

Two options:

### Option A — gcloud ADC (easiest for local dev)
```bash
brew install --cask google-cloud-sdk
gcloud auth application-default login
```
Select your Google account. No other config needed.

### Option B — Service account (for cron/server)
1. In Google Cloud Console → IAM & Admin → Service Accounts → Create
2. Grant it **Editor** on the Legolas spreadsheet (share the sheet with the service account email)
3. Create a JSON key → download it
4. Add to `~/.claude/.env`:
   ```
   GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json
   ```

## 4. Verify setup

```bash
cd ~/legolas
python3 debug.py
```

This runs without posting to Slack. Look for:
- `TAG SHEET SUMMARY` — should load tags from Google Sheet
- `FETCHING RSS` — should pull articles from feeds
- `CLAUDE ASSESSMENT` — should show tier decisions

Set `POST_TO_SLACK_DBG = True` at the top of `debug.py` to actually post.

## 5. Run for real

```bash
python3 legolas.py
```

Or schedule with cron (every 30 min):
```
*/30 * * * * cd ~/legolas && python3 legolas.py >> ~/legolas/legolas.log 2>&1
```

---

# Legolas V2 (story desk) — branch `rebuild-v2`

V2 runs alongside V1 (`legolas.py` is untouched apart from the feedback-channel fix);
the GitHub Actions workflow still runs V1 until it is switched over.

```bash
python -m legolas2 --mock --dry-run     # local check: real RSS, no Claude, no Slack
python -m legolas2                       # real Claude; Slack stays off unless config slack.post_enabled
python -m pytest                         # tests (no network, no key)
python replay/build_corpus.py . 2026-11-08T23:00:00Z replay/out/corpus.json
python replay/replay.py                  # $/day at 30/15/5-min cadence vs today's design
python -m scout.ledger summary --ledger state/cost-ledger.jsonl
```

Environment (never commit these):

| Variable | Used for |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude (key from a workspace with a spend limit) |
| `SLACK_BOT_TOKEN` | posting with threads for updates, and reading feedback |
| `LEGOLAS_POST_CHANNEL` | channel ID cards are posted to (#agent-legolas) |
| `LEGOLAS_FEEDBACK_CHANNEL` | channel ID feedback is read from (#agent-legolas); V1 uses it too |
| `SLACK_WEBHOOK` | fallback posting without threads |
| `LEGOLAS_TITLE_RULES_PATH` | private full title rules (contains MW data; not in this public repo) |
| `SCOUT_STATE_DIR` | where the story store, ledger and feed live (default `state/`, gitignored) |
| `LEGOLAS_KILL=1` / `SCOUT_KILL=1` | kill switch (also `state/killswitch.json`) |

Cost controls (all in `config/legolas-v2.yaml`): Haiku 5.5 for extraction / change
detection / feedback / verification, Sonnet 5.5 only for the top 5 cards per run, no
Opus; cached system prompts (1-hour TTL); daily cap $3 (`budget.daily_usd_cap`); web
search capped at 6/day and only for stories missing a second credible source. Every
call is in `state/cost-ledger.jsonl`.
