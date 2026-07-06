# Legolas — Reusable Technical Patterns

Business logic (RSS clustering, MW relevance scoring, tier rules) is intentionally excluded — this is the infrastructure/plumbing layer only, meant to be lifted into a new agent build.

---

## 1. Authentication & Secrets

**Two credential mechanisms, split by service type:**

| Service | Auth method | Where it lives |
|---|---|---|
| Claude (Anthropic) | Bearer API key in request header | `ANTHROPIC_API_KEY` env var |
| Slack (posting) | Incoming Webhook URL | `SLACK_WEBHOOK` env var |
| Slack (reading reactions/threads) | Bot token, `Authorization: Bearer` header | `SLACK_BOT_TOKEN` env var |
| Google Sheets | Service account, `google.auth.default()` | `GOOGLE_APPLICATION_CREDENTIALS` env var → path to JSON key |

**Local dev:** all four loaded via `python-dotenv` from a single file **outside the repo**: `~/.claude/.env`. Never a repo-local `.env`.

**CI (GitHub Actions):** secrets injected as workflow env vars from GitHub Secrets; the Google key is special-cased — it's written to a gitignored file at runtime rather than passed as a path to an existing file:
```yaml
- name: Write Google service account key
  run: echo '${{ secrets.GOOGLE_CREDENTIALS_JSON }}' > legolas-key.json
```
then `GOOGLE_APPLICATION_CREDENTIALS: legolas-key.json` is set for the run step. This lets one secret (`GOOGLE_CREDENTIALS_JSON`, the full JSON blob) become a file without ever committing that file — `legolas-key.json` and `.env` are both in `.gitignore`.

**Fail-fast pattern for required secrets:**
```python
def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

_require_env("ANTHROPIC_API_KEY", "SLACK_WEBHOOK", "SLACK_BOT_TOKEN")
```
Called once at module import time, right after `load_dotenv()`, before any other setup — crashes immediately and loudly rather than failing deep into a run with a confusing downstream error.

**Google Sheets service builder** — same pattern reusable for any Google API:
```python
def _sheets_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(google.auth.transport.requests.Request())
    return build("sheets", "v4", credentials=creds)
```
`google.auth.default()` transparently picks up either ADC (`gcloud auth application-default login`, for local dev) or the service-account JSON pointed to by `GOOGLE_APPLICATION_CREDENTIALS` (for CI) — no branching code needed for the two environments.

No Drive or BigQuery usage in this repo — only Sheets. If a new build needs Drive/BigQuery, swap the `scopes` list and the `build(...)` call; the credential-resolution part is identical.

---

## 2. GitHub Actions Scheduling

Single workflow file, `.github/workflows/legolas.yml`:

- **Multiple cron expressions in one `schedule` trigger** — GitHub Actions allows a list, used here for uneven-frequency coverage (dense during active hours, one extra late-night run):
  ```yaml
  on:
    schedule:
      - cron: '*/30 9-23 * * *'   # every 30 min, 9am–11:30pm UTC
      - cron: '0 2 * * *'          # once more at 2am UTC
    workflow_dispatch:             # manual trigger button in GH UI
  ```
- **`permissions: contents: write`** at the job level — required because the job commits back to the repo (state persistence, see below). Least-privilege: only `contents`, nothing else.
- **Step order**: checkout → setup-python (with pip cache) → install deps → materialize the Google key file → run the script with all secrets as env vars → commit state files back.
- **State persistence via git commit, not artifacts/external DB:**
  ```yaml
  - name: Commit updated learnings
    run: |
      git config user.name "Legolas Bot"
      git config user.email "legolas-bot@users.noreply.github.com"
      git add data/learnings.json data/seen.db
      git diff --staged --quiet || git commit -m "chore: update learnings + seen store [skip ci]"
      git push
  ```
  Two things worth reusing: (a) `git diff --staged --quiet ||` guards against empty commits when nothing changed, (b) `[skip ci]` in the commit message prevents the bot's own commit from retriggering anything that watches pushes.
- A dedicated bot identity (`Legolas Bot` / `legolas-bot@users.noreply.github.com`) is configured inline rather than relying on `github-actions[bot]`, making the commit history self-explanatory.

---

## 3. Slack Posting Pattern

- **Outbound posting = Incoming Webhook, Block Kit payload, always a new top-level message.** No threading of the bot's own posts, no message editing after posting (no `chat.update` calls anywhere in the codebase).
  ```python
  r = requests.post(SLACK_WEBHOOK, json={"text": fallback_text, "blocks": blocks}, timeout=15)
  r.raise_for_status()
  ```
  `text` is always populated as a plain-text fallback (for notifications/unfurl previews) even though `blocks` carries the real content — cheap and worth always doing.
- **Block Kit composed as a plain Python list, built incrementally**, mixing `section` (rich content) and `context` (small/muted metadata) block types:
  ```python
  blocks = []
  blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"...*{headline}*"}})
  blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "small metadata line"}]})
  blocks.append({"type": "divider"})
  ```
  Reusable convention: primary content → `section`, secondary/small metadata → `context`, and a trailing `divider` block to visually separate posts in a busy channel.
- **Reading feedback back (reactions + thread replies) uses the Bot Token against Web API endpoints, not the webhook**, since a webhook can only send, not read:
  - `conversations.history` — pull last N messages in the channel.
  - `conversations.replies` — pull a thread given its parent `ts`.
  - Each `msg["reactions"]` list is inspected for emoji names (`thumbsup`/`+1`, `thumbsdown`/`-1`).
- **Idempotency for feedback processing** — since `conversations.history` is re-scanned every run, already-processed reactions/replies are tracked in persisted state (`learnings["processed_reactions"]`, `["processed_replies"]`) keyed by `f"{ts}:{reaction_name}"` / `f"{parent_ts}:{reply_ts}"`, so nothing double-applies across runs.
- **No message-editing-in-place** — the pattern for "improve the message before it goes out" is a pre-post LLM audit step (this repo's "Aragorn" pass) that can rewrite/veto content *before* the `chat.postMessage`/webhook call, not an after-the-fact edit of a live Slack message. Simpler and avoids `chat.update` permission scope entirely.

---

## 4. Google Sheets Read/Write Pattern

- **One spreadsheet ID, many named tabs**, all IDs/names centralized in config (`agent-config.yaml`'s `sheets:` block) — never hardcoded in code.
- **Tab-name resolution is fuzzy on purpose** — a helper tries a list of acceptable names/casings so a human renaming a tab in the UI doesn't break the integration:
  ```python
  def find_tab(svc, names: List[str]) -> Optional[str]:
      tabs = {s["properties"]["title"] for s in svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()["sheets"]}
      for n in names:
          if n in tabs:
              return n
      return None
  ```
- **Read pattern** — `values().get(spreadsheetId=..., range=f"{tab}!A:A").execute().get("values", [])`, always defaulting to `[]` so an empty/missing tab degrades to "no data" rather than a crash.
- **Write pattern, two flavors depending on intent:**
  - **Ensure-tab-exists + header row**, run defensively every call (idempotent no-op if it already exists):
    ```python
    tabs = {...}
    if tab_name not in tabs:
        svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{"addSheet": {...}}]}).execute()
        svc.spreadsheets().values().update(spreadsheetId=SHEET_ID, range=f"{tab_name}!A1", valueInputOption="RAW", body={"values": [[...header...]]}).execute()
    ```
  - **Accumulate rows across runs** — always `.append()` with `insertDataOption="INSERT_ROWS"`, never `.update()`, so each run's writes land after existing data rather than overwriting:
    ```python
    svc.spreadsheets().values().append(spreadsheetId=SHEET_ID, range=f"{tab}!A:G", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": rows})
    ```
- **Batch, don't loop** — usage/seen/feedback rows are accumulated in memory during a run and written in one `.append()` call at the end, not one API call per record. Reduces API calls and avoids partial-write states if the process dies mid-run.
- **Sheets writes never block the pipeline** — every Sheets call site is wrapped in try/except that logs a warning and continues (see next section).

---

## 5. Error Handling & Logging Conventions

- **Logger setup**: `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", handlers=[FileHandler(...), StreamHandler()])`, retrieved everywhere via `log = logging.getLogger("<agent_name>")`. Both console and file output active by default — don't rely on `basicConfig`'s implicit default handler if you actually want a file; you must pass `handlers=` explicitly.
- **"Fail open" is the dominant philosophy** — anything that isn't the core value-delivery path (Sheets writes, Slack feedback reads, usage tracking) is wrapped in `try/except Exception as e: log.warning(...)`, and execution continues. Only missing required secrets (`_require_env`) and top-level unrecoverable errors should ever hard-stop a run.
- **Retry pattern for external API calls (Claude)** — bounded retries with exponential-ish backoff and explicit handling of rate limits:
  ```python
  for attempt in range(max_retries + 1):
      try:
          resp = requests.post(..., timeout=120)
          if resp.status_code == 429:
              time.sleep(retry_wait * (attempt + 1))
              continue
          resp.raise_for_status()
          return ...
      except Exception as e:
          log.warning(f"... (attempt {attempt+1}/{max_retries+1}): {e}")
          if attempt < max_retries:
              time.sleep(retry_wait)
  return None
  ```
  `max_retries` / `retry_wait_seconds` are config-driven, not hardcoded. Every external call has a `timeout` — none are unbounded.
- **Structured, greppable log lines for machine-parseable events** — e.g. `log.info("claude_usage call=%s input=%s output=%s ...", ...)` uses a stable `key=value` prefix specifically so these lines can be grepped/parsed later, distinct from prose log lines meant for humans.
- **Emoji-prefixed log lines for human-scannable run summaries** (`✅ Posted`, `⏭ Skip`, `🗡 KILL`, `✏️ rewrote`) — cheap, high-signal way to visually scan a long log for what happened without reading every line.

---

## 6. Repo & Code Structure Conventions

```
config/
  agent-config.yaml   # all tunable thresholds, IDs, tab names — never hardcoded in .py
  feeds.yaml           # source list, separate from behavior config
data/
  learnings.json       # persisted state, git-committed by CI each run
  seen.db              # SQLite persisted state, same commit cycle
.github/workflows/
  <agent>.yml           # single workflow file
<agent>.py              # everything: config load → fetch → process → post → run()
debug.py                # imports from <agent>.py, runs the same pipeline without posting/side-effects
setup.md                 # credential + local-dev instructions, human-facing
requirements.txt
.gitignore              # secrets, keys, caches, .DS_Store — never relaxed
docs/                    # dated change records (see below)
```

- **Single flat module** rather than a package — one `<agent>.py` with clearly divided `# ── Section ──` comment banners (Bootstrap, Config, Google Sheets auth, Claude API, Slack, Main run). Works well at this scale; would want to split into a package once it exceeds ~2–3k lines.
- **`debug.py` as a thin harness, not a copy** — imports named functions from the main module (`from legolas import fetch_rss_items, claude_assess_clusters, ...`) and re-runs the real pipeline with side-effects (posting, writing) disabled via a module-level flag (`POST_TO_SLACK_DBG = False`). Avoids duplicated/drifting logic between "real" and "debug" paths.
- **Naming conventions:**
  - `_leading_underscore` = private/internal helper or module-level cache, not meant to be imported elsewhere.
  - `ALL_CAPS` = config-derived constants, loaded once at import time from YAML (e.g. `SLACK_CHANNEL_ID = _SLACK_CFG["channel_id"]`) — config is read once, then treated as constants for the rest of the process lifetime.
  - `claude_*` prefix = functions that make an LLM call and return a judgment/decision (`claude_find_merges`, `claude_assess_clusters`) — visually distinguishes "Claude decides" functions from mechanical Python functions, reinforcing the "Python picks nothing" philosophy at the naming level.
  - `load_*` / `save_*` = state or config I/O.
  - `_sheets_service`, `_call_claude` = the two "how do I talk to an external service" chokepoints — every Sheets or Claude call in the codebase funnels through one of these two functions, making it trivial to add cross-cutting behavior (like the usage-tracking buffer) in exactly one place.
- **Config vs. code boundary is strict**: thresholds, IDs, tab names, retry counts — all in `agent-config.yaml`/`feeds.yaml`, loaded once into module-level constants. Nothing that a non-engineer might reasonably want to tune is hardcoded in `.py`.
- **`docs/` for audit-ready, dated change records** (`YYYY-MM-DD-<topic>.md`) — a lightweight paper trail separate from commit messages, useful for anything a security/audit review might ask about later (see the usage-tracking doc from earlier this session as the template).
