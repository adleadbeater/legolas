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
