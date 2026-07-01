# Change Record: Claude API Usage Tracking Fix

**System:** Legolas (MW entertainment news scout)
**Date:** 2026-07-01
**Author:** Alex Leadbeater, with Claude Code assistance
**Type:** Diagnostic fix + new monitoring capability (no change to editorial logic)

---

## 1. Issue Reported

Usage-tracking log lines (`claude_usage ...`), added earlier the same day to record
token counts for every Claude API call, were not appearing in `legolas.log` as
expected.

## 2. Investigation

A structured, step-by-step diagnosis was performed:

1. **Confirmed the logging call itself was present and correctly written** in
   `_call_claude()` — the function that wraps every outbound Claude API request.
   It logs `call_label`, `input_tokens`, `output_tokens`, and cache token counts
   on every successful response.
2. **Confirmed the log level was not the cause** — the logger was correctly
   configured at `INFO` level, and `INFO` calls were not being filtered.
3. **Found the root cause**: Python's logging setup
   (`logging.basicConfig(...)`) had no file handler — only a console/stderr
   handler. The application had *never* written to `legolas.log` from any
   automated run; the file's existing content was a leftover from a one-off
   manual run weeks earlier, where a person had manually redirected console
   output to a file by hand. Every automated run since then (via GitHub
   Actions) logged only to the ephemeral CI console, which is not retained
   in the repository.
4. **Confirmed no other part of the pipeline (the GitHub Actions workflow)
   redirects or commits log output** — the automated workflow only commits two
   data files (`data/learnings.json`, `data/seen.db`); it was never wired to
   persist `legolas.log`.

**Conclusion:** the usage-logging code was correct; the surrounding logging
infrastructure was incomplete. This was a configuration gap, not a logic bug —
no editorial/scoring behavior was affected.

## 3. Immediate Fix

- Added a proper file handler so `INFO`-level messages (including
  `claude_usage`) now write to `legolas.log` on every run, alongside the
  existing console output.
- Removed `legolas.log` from version control (it was previously committed to
  the repo, growing unbounded — 9+ MB at time of review — and was never
  automatically refreshed). It is now listed in `.gitignore`.

## 4. Follow-up Decision: How Usage Should Actually Be Tracked

A plain text log file is not a good long-term home for usage data anyway — it's
not queryable, and committing a growing log to git bloats repository history
permanently (deleting a large file later does not shrink existing git history).

**Options considered:**

| Option | Where it lives | Repo impact | Queryable? |
|---|---|---|---|
| A — Rotating local log file only | Local disk | None (gitignored) | No — text search only |
| B — Small CSV/JSONL committed to repo | `data/` in git | Small, grows slowly (~1MB/year) | Yes, via download |
| **C — Append to a Google Sheet tab (chosen)** | Google Sheets | None | Yes, live, in a tool already used for reporting |
| D — Committed raw log, rotated | Git | Ongoing churn, history bloat | Text search only |

**Decision: Option C.** Legolas already authenticates to Google Sheets for
existing reporting (tag-performance lookups). Reusing that connection to write
a "Usage" tab avoids any new credential, any new infrastructure, and any git
footprint, while giving live, chartable visibility into Claude API usage
across runs.

## 5. What Was Implemented

- Each Claude API call already records: timestamp (UTC), which call it was
  (e.g. "merge", "assess", "feedback", "synthesize", "aragorn"), input tokens,
  output tokens, cache-write tokens, cache-read tokens, and model name.
- These records are held in memory for the duration of a single run and
  written to a new **"Usage" tab** in the existing Google Sheet in one batch
  request at the end of each run (not one API call per Claude call — reduces
  API overhead and avoids partial-write inconsistency).
- If the Sheets write fails for any reason (e.g. transient network issue),
  the run is **not** blocked or failed — the error is logged and the run
  continues normally. This follows the project's existing pattern of never
  letting a monitoring/logging failure take down the actual news-scouting
  pipeline.
- No credentials were added, embedded, or logged. The existing Google service
  account credential (already scoped to this Sheet) is reused; no new access
  scope was requested.

## 6. Risk & Access Summary (for audit purposes)

- **Data involved:** token counts and call labels only — no article content,
  no PII, no customer data, no secrets.
- **New credentials/secrets:** none.
- **New network destinations:** none — writes go to the same Google Sheet the
  system already reads from and writes to.
- **New permissions/access scope:** none — reuses the existing service account
  and existing spreadsheet ID.
- **Failure mode:** fails open (logs a warning, does not stop the pipeline) —
  usage tracking can never cause a missed run or a bad post.
- **Reversibility:** fully reversible; the new tab and code path can be
  removed without affecting any editorial/scoring logic.

## 7. Verification

- Code compiles cleanly (`py_compile`).
- Manual trace of the call path confirms every `_call_claude()` invocation
  (5 call sites: merge, assess, feedback synthesis, editorial-notes synthesis,
  Aragorn audit) now both logs to file and buffers a usage record.
- Next scheduled run (GitHub Actions, every 30 min) will be the first live
  end-to-end confirmation; recommend spot-checking the new "Usage" tab in the
  Sheet after the next run to confirm rows appear as expected.

---

## Adapting This for Other Chats/Systems (Paperboy, Boromir, Faramir, etc.)

This record is written to be reusable. To adapt it for another system:

1. Replace "Legolas" / "MW entertainment news scout" with the target system's
   name and purpose.
2. Re-run the same 4-step diagnostic checklist (code present? handler
   configured? level correct? recent log entries prove/disprove it's live?)
   against that system's actual logging setup — do not assume the same root
   cause applies without checking.
3. Re-evaluate the options table against that system's existing
   infrastructure — e.g. if a system doesn't already use Google Sheets,
   Option C's "no new credential" advantage disappears, and Option A or B may
   be the better default.
4. Keep the Risk & Access Summary section — auditors will want that shape
   (data involved, credentials, network destinations, permissions, failure
   mode, reversibility) regardless of which system this describes.
