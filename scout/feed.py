"""Story feed: the JSON-line contract shared by Legolas, the Elves and Celebrimbor.

One line per write; a story's row is identified by `id` and versioned by `rev`, so an
update in place is a new line with the same id and a higher rev (readers keep the
latest rev). `reject_reason` is OPTIONAL: editors can reject with no reason.
Structured columns only - the dashboard renders them as plain columns.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

FEED_VERSION = 1

REQUIRED = {"v", "id", "rev", "agent", "title", "what_happened", "first_seen", "updated_at",
            "status", "mw_status", "sources", "source_tier"}
OPTIONAL = {"elf", "topic", "topic_kind", "event_type", "summary", "angle", "angles",
            "entities", "mw_tags", "mw_match", "why_mw", "confidence", "score", "tier_label",
            "momentum", "also_topics", "follow_up_of", "update_note", "reject_reason",
            "reject_note", "url", "related", "story_type", "source_type", "proven_angle",
            "rumour", "auto_approve", "cost_usd"}
STATUSES = {"new", "vetted", "in_asana", "published", "rejected", "covered", "updated"}
REJECT_REASONS = {"already_on_mw", "wrong_topic", "weak_or_unsourced", "not_our_audience",
                  "too_late", "duplicate_of_another_agent", "not_a_story", "facts_wrong",
                  "bad_angle", None}
MW_STATUSES = {"not_on_mw", "follow_up", "covered"}


def validate(row: dict) -> list:
    errs = [f"missing {k}" for k in REQUIRED if k not in row]
    unknown = set(row) - REQUIRED - OPTIONAL
    errs += [f"unknown field {k}" for k in sorted(unknown)]
    if row.get("status") not in STATUSES:
        errs.append(f"bad status {row.get('status')!r}")
    if row.get("mw_status") not in MW_STATUSES:
        errs.append(f"bad mw_status {row.get('mw_status')!r}")
    if row.get("reject_reason") not in REJECT_REASONS:
        errs.append(f"bad reject_reason {row.get('reject_reason')!r}")
    if not str(row.get("what_happened") or "").strip():
        errs.append("what_happened is required")
    if not isinstance(row.get("sources"), list):
        errs.append("sources must be a list")
    return errs


class FeedWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, row: dict) -> dict:
        row = {"v": FEED_VERSION, **row}
        errs = validate(row)
        if errs:
            raise ValueError("feed row invalid: " + "; ".join(errs))
        with open(self.path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def latest(self) -> dict:
        out = {}
        if self.path.exists():
            for line in open(self.path):
                r = json.loads(line)
                if r["id"] not in out or r["rev"] >= out[r["id"]]["rev"]:
                    out[r["id"]] = r
        return out


def reject(writer: FeedWriter, row_id: str, reason: Optional[str] = None,
           note: Optional[str] = None, updated_at: str = "") -> dict:
    """Editor reject. The reason is optional by design."""
    cur = writer.latest()[row_id]
    new = {**cur, "rev": cur["rev"] + 1, "status": "rejected", "reject_reason": reason,
           "updated_at": updated_at or cur["updated_at"]}
    if note:
        new["reject_note"] = note
    new.pop("v", None)
    return writer.write(new)
