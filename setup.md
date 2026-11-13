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

# Legolas V2 (story desk) — live from branch `approved-13nov`

Approved 13 Nov 2026: the GitHub Actions workflow runs **V2** (`python -m legolas2 --post`)
every 15 minutes, 5am–10pm ET, with its state (story store, seen-set, cost ledger, story
feed) kept in the Actions cache, not committed. Spend is capped at **$1/day**. Google News
is no longer a source (RSS feeds in `config/feeds.yaml` only). V1 (`legolas.py`) stays
in the repo but no longer runs; its title rules now also load from the private secret.

```bash
python -m legolas2 --mock --dry-run     # local check: real RSS, no Claude, no Slack
python -m legolas2                       # real Claude; Slack stays off unless --post
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
| `LEGOLAS_TITLE_RULES_B64` | private full title rules, base64 (GitHub secret; contains MW data, never in this public repo) |
| `LEGOLAS_TITLE_RULES` / `LEGOLAS_TITLE_RULES_PATH` | same rules as plain text / a local file path (laptop runs) |
| `GOOGLE_CREDENTIALS_JSON` | (Actions secret) service account JSON, written to a temp file outside the repo |
| `SCOUT_STATE_DIR` | where the story store, ledger and feed live (default `state/`, gitignored) |
| `LEGOLAS_KILL=1` / `SCOUT_KILL=1` | kill switch (also `state/killswitch.json`; in Actions set the repo **variable** `LEGOLAS_KILL` to `1`) |

Cost controls (all in `config/legolas-v2.yaml`): Haiku 5.5 for extraction / change
detection / feedback / verification, Sonnet 5.5 only for the top 5 cards per run, no
Opus; cached system prompts (1-hour TTL); daily cap **$1** (`budget.daily_usd_cap`; when it
is reached the run stops, posts nothing and logs a `budget_stop` event); web
search capped at 6/day and only for stories missing a second credible source. Every
call is in `state/cost-ledger.jsonl`.
