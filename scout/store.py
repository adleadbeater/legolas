"""Story store (SQLite, outside git). Shared by Legolas, the Elves and Celebrimbor.

Tables
  items        every article ever fetched = the persisted seen-set. Extraction output
               is cached per article, so no article is ever sent to a model twice.
  stories      one row per story, updated in place (entities, facts timeline, sources,
               MW status, momentum, posting state). `lanes` records which agents /
               topics have claimed it (V3: Elves tag a Legolas row instead of posting
               a second one).
  mw_articles  MW's own recent articles (from the public news sitemap) for the
               coverage check.
  counters     per-day counters (web searches per lane, updates per story...).
  kv           small state (pending batch ids, last run time).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  guid TEXT PRIMARY KEY, title TEXT, url TEXT, source_name TEXT, source_tier INT,
  published_at TEXT, seen_at TEXT, text TEXT, via TEXT,
  extraction TEXT, extracted_at TEXT, story_id TEXT
);
CREATE INDEX IF NOT EXISTS items_story ON items(story_id);
CREATE INDEX IF NOT EXISTS items_pub ON items(published_at);
CREATE INDEX IF NOT EXISTS items_title ON items(source_name, title);
CREATE TABLE IF NOT EXISTS stories (
  id TEXT PRIMARY KEY, data TEXT NOT NULL, last_item_at TEXT, state TEXT
);
CREATE INDEX IF NOT EXISTS stories_last ON stories(last_item_at);
CREATE TABLE IF NOT EXISTS mw_articles (url TEXT PRIMARY KEY, title TEXT, published_at TEXT);
CREATE TABLE IF NOT EXISTS counters (day TEXT, key TEXT, n INT, PRIMARY KEY(day, key));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ── items / seen-set ─────────────────────────────────────────────────────
    def unseen(self, items: Iterable[dict]) -> List[dict]:
        out, batch = [], list(items)
        seen = set()
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            q = "SELECT guid FROM items WHERE guid IN (%s)" % ",".join("?" * len(chunk))
            seen.update(r[0] for r in self.db.execute(q, [c["guid"] for c in chunk]))
        dupes = set()
        for it in batch:
            tkey = (it["source_name"], it["title"].strip())
            if it["guid"] in seen or it["guid"] in dupes or tkey in dupes:
                continue
            # same outlet re-publishing the same headline under a new guid
            if self.db.execute("SELECT 1 FROM items WHERE source_name=? AND title=? LIMIT 1", tkey).fetchone():
                continue
            dupes.add(it["guid"])
            dupes.add(tkey)
            out.append(it)
        return out

    def add_items(self, items: Iterable[dict], now: datetime) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO items (guid,title,url,source_name,source_tier,published_at,seen_at,text,via)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [(i["guid"], i["title"], i.get("url", ""), i["source_name"], i.get("source_tier", 3),
              _iso(i["published_dt"]), _iso(now), i.get("text", ""), i.get("via", "rss")) for i in items])
        self.db.commit()

    def set_extraction(self, guid: str, extraction: dict, now: datetime) -> None:
        self.db.execute("UPDATE items SET extraction=?, extracted_at=? WHERE guid=?",
                        (json.dumps(extraction), _iso(now), guid))

    def set_item_story(self, guid: str, story_id: str) -> None:
        self.db.execute("UPDATE items SET story_id=? WHERE guid=?", (story_id, guid))

    def commit(self) -> None:
        self.db.commit()

    def items_for_story(self, story_id: str) -> List[dict]:
        rows = self.db.execute(
            "SELECT guid,title,url,source_name,source_tier,published_at,text,extraction FROM items"
            " WHERE story_id=? ORDER BY published_at", (story_id,)).fetchall()
        return [{"guid": r[0], "title": r[1], "url": r[2], "source_name": r[3], "source_tier": r[4],
                 "published_dt": _dt(r[5]), "text": r[6],
                 "extraction": json.loads(r[7]) if r[7] else None} for r in rows]

    def prune(self, now: datetime, item_days: int = 14) -> None:
        cut = _iso(now - timedelta(days=item_days))
        self.db.execute("DELETE FROM items WHERE seen_at < ?", (cut,))
        self.db.execute("DELETE FROM stories WHERE last_item_at < ?", (cut,))
        self.db.execute("DELETE FROM mw_articles WHERE published_at < ?", (cut,))
        self.db.commit()

    # ── stories ──────────────────────────────────────────────────────────────
    def new_story(self, now: datetime, lane: str = "legolas") -> dict:
        return {
            "id": "st-" + uuid.uuid4().hex[:10], "created_at": _iso(now), "updated_at": _iso(now),
            "lanes": [lane], "event_type": "other", "entities": [], "primary_entity": "",
            "key_fact": "", "facts": [], "relevance": 0, "is_news": False, "rumour": False,
            "state": "open", "first_seen": _iso(now), "last_item_at": _iso(now),
            "posted_at": None, "last_post_at": None, "posts": 0, "card": None,
            "carded_facts": [], "slack_ts": None, "feed_rev": 0, "mw_status": None,
            "mw_match": None, "topics": [], "related": [], "momentum": 0.0,
        }

    def save_story(self, s: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO stories (id,data,last_item_at,state) VALUES (?,?,?,?)",
                        (s["id"], json.dumps(s), s["last_item_at"], s["state"]))

    def get_story(self, sid: str) -> Optional[dict]:
        r = self.db.execute("SELECT data FROM stories WHERE id=?", (sid,)).fetchone()
        return json.loads(r[0]) if r else None

    def open_stories(self, now: datetime, hours: int) -> List[dict]:
        cut = _iso(now - timedelta(hours=hours))
        return [json.loads(r[0]) for r in
                self.db.execute("SELECT data FROM stories WHERE last_item_at >= ?", (cut,))]

    def recent_stories(self, now: datetime, days: int) -> List[dict]:
        return self.open_stories(now, days * 24)

    # ── MW articles ──────────────────────────────────────────────────────────
    def add_mw_articles(self, arts: Iterable[dict]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO mw_articles (url,title,published_at) VALUES (?,?,?)",
                            [(a["url"], a["title"], _iso(a["published_dt"])) for a in arts])
        self.db.commit()

    def mw_articles(self, now: datetime, days: int) -> List[dict]:
        cut = _iso(now - timedelta(days=days))
        return [{"url": r[0], "title": r[1], "published_dt": _dt(r[2])} for r in self.db.execute(
            "SELECT url,title,published_at FROM mw_articles WHERE published_at >= ? AND published_at <= ?",
            (cut, _iso(now)))]

    # ── counters / kv ────────────────────────────────────────────────────────
    def count(self, day: str, key: str) -> int:
        r = self.db.execute("SELECT n FROM counters WHERE day=? AND key=?", (day, key)).fetchone()
        return r[0] if r else 0

    def incr(self, day: str, key: str, by: int = 1) -> int:
        self.db.execute("INSERT INTO counters (day,key,n) VALUES (?,?,?) ON CONFLICT(day,key)"
                        " DO UPDATE SET n=n+?", (day, key, by, by))
        self.db.commit()
        return self.count(day, key)

    def kv_get(self, key: str, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key,value) VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()
