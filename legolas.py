"""
Legolas Phase 1 — News Scout (Claude edition v2)
Architecture:
  1. Python: RSS fetch → hard filter → keyword cluster
  2. Claude: re-cluster (merge dupes, split incoherent) → bucket → quality assess
  3. Python: enforce mechanical rules (source counts, mw threshold) → post

Run: python legolas.py
Debug: python debug.py

Credentials: ~/.claude/.env
  ANTHROPIC_API_KEY=...
  SLACK_WEBHOOK=...
  SLACK_BOT_TOKEN=...
  GOOGLE_APPLICATION_CREDENTIALS=...  (optional — falls back to ADC)
"""

import logging
import os
import re
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional, List, Dict, Tuple, Set
from pathlib import Path

import requests
import feedparser
import yaml
from dotenv import load_dotenv
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build

# ── Bootstrap ──────────────────────────────────────────────────────────────────
load_dotenv(Path.home() / ".claude" / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("legolas")

_DIR = Path(__file__).parent

# ── Config ─────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    with open(_DIR / "config" / "agent-config.yaml") as f:
        return yaml.safe_load(f)

def _load_feeds() -> Tuple[dict, dict]:
    with open(_DIR / "config" / "feeds.yaml") as f:
        data = yaml.safe_load(f)
    tier1 = {e["name"]: e["url"] for e in data.get("tier1", [])}
    tier2 = {e["name"]: e["url"] for e in data.get("tier2", [])}
    return tier1, tier2

_CFG          = _load_config()
TIER_1_SOURCES, TIER_2_SOURCES = _load_feeds()
ALL_SOURCES   = {**TIER_1_SOURCES, **TIER_2_SOURCES}

_SCORING      = _CFG["scoring"]
_CLAUDE_CFG   = _CFG["claude"]
_SHEETS_CFG   = _CFG["sheets"]
_SLACK_CFG    = _CFG["slack"]

CLAUDE_MODEL        = _CLAUDE_CFG["model"]
MW_RELEVANCE_MIN    = _SCORING["mw_relevance_min"]
LEGOLAS_SPECIAL_MIN = _SCORING["legolas_pick_min_relevance"]
TAG_SA_MIN          = _SCORING["tag_sa_min"]
TAG_FR_MAX          = _SCORING["tag_fr_max"]
CACHE_HOURS         = _SCORING["article_cache_hours"]
RECENTLY_POSTED_HRS = _SCORING["recently_posted_hours"]
LOOKBACK_MINS       = _SCORING["lookback_minutes"]

SHEET_ID     = _SHEETS_CFG["sheet_id"]
SLACK_CHANNEL_ID = _SLACK_CFG["channel_id"]
POST_TO_SLACK    = _SLACK_CFG["post_enabled"]

def _require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(
            f"\n❌ Missing credentials in ~/.claude/.env:\n"
            + "\n".join(f"  {n}=..." for n in missing)
            + "\n\nSee setup.md for instructions."
        )

_require_env("ANTHROPIC_API_KEY", "SLACK_WEBHOOK", "SLACK_BOT_TOKEN")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK     = os.environ["SLACK_WEBHOOK"]
SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]

_PUB_GROUPS = _CFG.get("publisher_groups", {})
# source name -> corporate parent, for outlets that count as 0.5 toward the
# trending threshold (so one parent company can't trigger Big Trend alone).
_HALF_WEIGHT_SOURCE = {
    src: parent
    for parent, srcs in _CFG.get("corporate_half_weight", {}).items()
    for src in srcs
}
_TIER2_SET  = set(TIER_2_SOURCES.keys())
_GENERIC_TAGS = set(_CFG.get("generic_tag_blocklist", []))
# Common English words that also appear as MW sheet tags — rejected as priority
# tags because substring matching produces false MW Proven Topics.
_TAG_STOPWORDS = {t.lower() for t in _CFG.get("tag_stopword_blocklist", [
    "it", "you", "your", "men", "women", "man", "her", "him", "his", "she", "they",
    "hope", "troy", "us", "we", "now", "new", "one", "two", "all", "out", "up",
    "down", "off", "on", "in", "the", "and", "for", "but", "not", "yes", "go",
])}

LEARNINGS_PATH = _DIR / "data" / "learnings.json"

# ── Google Sheets auth ─────────────────────────────────────────────────────────
def _sheets_service():
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return build("sheets", "v4", credentials=creds)

def find_tab(svc, names: List[str]) -> Optional[str]:
    tabs = {s["properties"]["title"] for s in
            svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()["sheets"]}
    for n in names:
        if n in tabs:
            return n
    return None

# ── Tag sheet ──────────────────────────────────────────────────────────────────
def load_tag_performance(svc) -> dict:
    tab = find_tab(svc, _SHEETS_CFG["tag_tab_names"])
    if not tab:
        log.warning("No Tag tab found in sheet")
        return {}
    rows = (svc.spreadsheets().values()
            .get(spreadsheetId=SHEET_ID, range=f"{tab}!A:Z")
            .execute().get("values", []))
    if not rows:
        return {}

    headers = [h.strip().lower() for h in rows[0]]

    def col(name):
        for i, h in enumerate(headers):
            if name in h:
                return i
        return None

    tag_col = col("tag")
    sa_col  = col("s/a") or col("sa") or col("search")
    fr_col  = col("fr") or col("fail") or col("rate")
    pts_col = col("pts") or col("point") or col("score")

    if tag_col is None:
        return {}

    priority = {}
    for row in rows[1:]:
        if len(row) <= tag_col:
            continue
        tag = row[tag_col].strip().lower()
        if not tag:
            continue
        try:
            sa  = float(str(row[sa_col]).replace(",", "")) if sa_col and sa_col < len(row) else 0
            fr  = float(str(row[fr_col]).replace("%", "")) if fr_col and fr_col < len(row) else 0
            pts = int(row[pts_col]) if pts_col and pts_col < len(row) and row[pts_col] else 0
        except (ValueError, IndexError):
            continue
        if _is_junk_tag(tag):
            continue
        if sa >= TAG_SA_MIN and fr < TAG_FR_MAX:
            priority[tag] = {"sa": sa, "fr": fr, "pts": pts}

    log.info(f"Loaded {len(priority)} priority tags (S/A ≥ {TAG_SA_MIN:,}, FR < {TAG_FR_MAX}%)")
    return priority


def _is_junk_tag(tag: str) -> bool:
    """
    Reject priority tags that are too generic to safely match by substring.
    These produce false MW Proven Topics — e.g. the tag 'it' matching
    "It's Not TV", 'v' matching "England v Ghana", '|' matching "...| Video".
    A tag must be a specific title/franchise/person, not a common word.
    """
    t = tag.strip().lower()
    if len(t) <= 2:                                   # 'it', 'v', 'us'
        return True
    if not any(ch.isalnum() for ch in t):             # '|', '-', punctuation
        return True
    # Single-token common English words that are also MW sheet tags.
    if " " not in t and t in _TAG_STOPWORDS:
        return True
    return False

# ── Seen GUID management ───────────────────────────────────────────────────────
def load_seen_guids(svc) -> set:
    try:
        tab = find_tab(svc, [_SHEETS_CFG["seen_tab_name"], "seen"])
        rows = (svc.spreadsheets().values()
                .get(spreadsheetId=SHEET_ID, range=f"{tab}!A:A")
                .execute().get("values", []))
        guids = {r[0] for r in rows if r}
        log.info(f"Loaded {len(guids)} seen GUIDs")
        return guids
    except Exception as e:
        log.warning(f"Could not load seen GUIDs: {e}")
        return set()

def ensure_seen_tab(svc):
    tabs = {s["properties"]["title"] for s in
            svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()["sheets"]}
    if _SHEETS_CFG["seen_tab_name"] not in tabs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": _SHEETS_CFG["seen_tab_name"]}}}]}
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{_SHEETS_CFG['seen_tab_name']}!A1",
            valueInputOption="RAW",
            body={"values": [["guid", "title", "source", "pubDate", "timestamp_added"]]}
        ).execute()
        log.info("Created Seen tab")

def append_seen_guids(svc, items: List[dict]):
    if not items:
        return
    tab = find_tab(svc, [_SHEETS_CFG["seen_tab_name"], "seen"]) or _SHEETS_CFG["seen_tab_name"]
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        [i["guid"], i["title"][:120], i["source_name"], i["published_dt"].isoformat(), now]
        for i in items
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range=f"{tab}!A:E",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()
    log.info(f"Appended {len(rows)} GUIDs to Seen tab")

# ── Learnings ──────────────────────────────────────────────────────────────────
def load_learnings() -> dict:
    try:
        with open(LEARNINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_learnings(learnings: dict):
    try:
        LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARNINGS_PATH, "w") as f:
            json.dump(learnings, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save learnings: {e}")

# ── RSS fetch ──────────────────────────────────────────────────────────────────
def fetch_rss_items(sources: dict, lookback_mins: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_mins)
    items  = []
    for source_name, feed_url in sources.items():
        try:
            resp = requests.get(feed_url, timeout=15, headers={"User-Agent": "Legolas/2.0"})
            resp.raise_for_status()
            feed  = feedparser.parse(resp.content)
            count = 0
            for entry in feed.entries:
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                else:
                    pub_dt = datetime.now(timezone.utc)
                if pub_dt < cutoff:
                    continue
                guid  = getattr(entry, "id", None) or getattr(entry, "link", None) or entry.get("title", "")
                title = getattr(entry, "title", "").strip()
                url   = getattr(entry, "link", "")
                if not title:
                    continue
                items.append({
                    "guid":         guid,
                    "title":        title,
                    "summary":      (getattr(entry, "summary", "") or "")[:400],
                    "url":          url,
                    "published_dt": pub_dt,
                    "source_name":  source_name,
                    "source_tier":  1 if source_name in TIER_1_SOURCES else 2,
                })
                count += 1
            log.info(f"[T{1 if source_name in TIER_1_SOURCES else 2}] {source_name:<25} {count} items")
        except Exception as e:
            log.warning(f"Failed to fetch {source_name}: {e}")
    log.info(f"Total fetched: {len(items)}")
    return items

# ── Hard content filter ────────────────────────────────────────────────────────
_SKIP_RE = re.compile(
    r"\b(switch\s*2|xbox|playstation\s*5|ps5|nintendo|steam sale|pre.?order"
    r"|discount|deals?|buy now|save \$|power station|projector|3d printer"
    r"|nba|nfl|mlb|nhl|fifa|premier league|champions league|f1 racing"
    r"|stock market|earnings report|quarterly results"
    r"|obituary|dies at \d+|passed away)\b",
    re.IGNORECASE,
)
_ENTERTAINMENT_RE = re.compile(
    r"\b(actor|actress|director|producer|writer|showrunner|singer|musician"
    r"|filmmaker|comedian|star|cast|oscar|emmy|grammy)\b",
    re.IGNORECASE,
)

# Listicle/guide title patterns — these articles mention multiple unrelated titles or
# are roundup guides that poison clustering and add no news value
_LISTICLE_TITLE_RE = re.compile(
    r"\b(\d+\s+(?:movies?|films?|shows?|series|titles?|picks?|reasons?|things?)"
    r"|movies? (we|you|to) (can't wait|must|need|should|watch|see)"
    r"|best (?:movies?|shows?|series|films?) (?:of|to|on|coming)"
    r"|most anticipated (?:movies?|shows?|films?)"
    r"|what to watch|leaving (?:this month|in \w+)"
    r"|ranked|ranking|countdown"
    r"|everything (we|you) know|all (the )?details|explained|premiere date.*details"
    r"|complete guide|what to expect|release date.*details)\b",
    re.IGNORECASE,
)

# Listicle URL path segments used by major entertainment outlets
_LISTICLE_URL_RE = re.compile(
    r"/(lists?|rankings?|best-of|top-\d|roundup|guide|preview-\d|anticipated)/",
    re.IGNORECASE,
)

_MAX_SINGLE_SOURCE_AGE_HRS = 24

def filter_items(items: list) -> list:
    filtered = []
    for item in items:
        if _SKIP_RE.search(item["title"]) and not _ENTERTAINMENT_RE.search(item["title"]):
            continue
        # Drop listicle/roundup articles — they bridge unrelated stories at clustering time
        if _LISTICLE_TITLE_RE.search(item["title"]):
            log.debug(f"Listicle title filtered: {item['title'][:80]}")
            continue
        if _LISTICLE_URL_RE.search(item.get("url", "")):
            log.debug(f"Listicle URL filtered: {item.get('url', '')[:80]}")
            continue
        filtered.append(item)
    log.info(f"Filter: {len(items)} → {len(filtered)} items")
    return filtered

def filter_old_single_source_clusters(clusters: list) -> list:
    now = datetime.now(timezone.utc)
    cutoff = timedelta(hours=_MAX_SINGLE_SOURCE_AGE_HRS)
    kept = []
    for c in clusters:
        unique_sources = len(set(it["source_name"] for it in c["items"]))
        if unique_sources == 1:
            age = now - min(it["published_dt"] for it in c["items"] if not it.get("_from_cache"))  if any(not it.get("_from_cache") for it in c["items"]) else timedelta(0)
            if age > cutoff:
                log.info(f"Dropped single-source cluster older than {_MAX_SINGLE_SOURCE_AGE_HRS}h: {c['headline'][:60]}")
                continue
        kept.append(c)
    return kept

# ── Keyword extraction ─────────────────────────────────────────────────────────
_STOP = {
    "the","a","an","and","or","but","in","on","at","to","for","of","with",
    "by","from","as","is","was","are","be","been","being","have","has","had",
    "do","does","did","will","would","could","should","may","might","shall",
    "that","this","these","those","it","its","he","she","they","we","you",
    "his","her","their","our","your","my","who","what","when","where","how",
    "new","big","best","top","first","last","year","time","show","film","movie",
    "series","season","episode","star","stars","actor","actress","director",
    "review","trailer","watch","streaming","netflix","disney","hbo","amazon",
    "about","after","before","during","over","under","between","into","through",
    "why","all","just","now","out","up","down","back","off","still","also",
    "not","no","one","two","three","more","most","some","any","good","great",
}
_SINGLE_STOP = _STOP | {
    "says","said","told","reveals","shares","opens","talks","explains","teases",
    "cast","role","part","play","playing","played","plays","set","sets",
    "joins","joined","join","returns","return","returned","coming","goes",
    "makes","made","make","gets","got","get","gives","give","gave",
    "based","has","season","series","show","film","movie","finale","premiere",
    "episode","special","announces","announced","announce","reveals","revealed",
}

_QUOTE_CHARS = chr(0x2018) + chr(0x2019) + chr(0x201c) + chr(0x201d) + "'"
_QUOTE_OPEN  = '[' + _QUOTE_CHARS + ']'
_QUOTE_BODY  = '[^' + _QUOTE_CHARS + ']{3,40}'
_QUOTED_RE   = re.compile(_QUOTE_OPEN + '(' + _QUOTE_BODY + ')' + _QUOTE_OPEN)

def _extract_keywords(text: str) -> set:
    keywords = set()
    for title in _QUOTED_RE.findall(text):
        clean = title.strip()
        if len(clean) > 2:
            keywords.add("TITLE:" + clean.lower())
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text):
        phrase = m.group(1)
        if not any(w in _STOP for w in phrase.lower().split()):
            keywords.add(phrase.lower())
    for w in re.findall(r"\b[A-Z][a-z]{4,}\b", text):
        if w.lower() not in _SINGLE_STOP:
            keywords.add(w.lower())
    return keywords

# ── Python keyword clustering ──────────────────────────────────────────────────
def cluster_items(items: list) -> list:
    """
    Fast keyword-overlap clustering. Imperfect — Claude fixes merges/splits in Call 1.
    Thresholds by keyword type (from agent-config.yaml clustering section):
      - title phrases (TITLE:*): 1 match
      - multi-word proper nouns: 1 match
      - single capitalised words: 3 matches
      - cross-type (multi from one + single from other): 2 matches
    """
    processed = [{**item, "keywords": _extract_keywords(item["title"])} for item in items]
    n = len(processed)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(n):
        for j in range(i + 1, n):
            a, b = processed[i]["keywords"], processed[j]["keywords"]
            title_a  = {k for k in a if k.startswith("TITLE:")}
            title_b  = {k for k in b if k.startswith("TITLE:")}
            multi_a  = {k for k in a if " " in k and not k.startswith("TITLE:")}
            multi_b  = {k for k in b if " " in k and not k.startswith("TITLE:")}
            single_a = {k for k in a if " " not in k and not k.startswith("TITLE:")}
            single_b = {k for k in b if " " not in k and not k.startswith("TITLE:")}
            cross    = (multi_a & single_b) | (single_a & multi_b)
            if (title_a & title_b) or (multi_a & multi_b) or len(single_a & single_b) >= 3:
                union(i, j)
            elif len(cross) >= 2:
                union(i, j)

    groups = defaultdict(list)
    for i, item in enumerate(processed):
        groups[find(i)].append(item)

    clusters = []
    for _, group_items in groups.items():
        group_items.sort(key=lambda x: x["published_dt"])
        sources  = list(dict.fromkeys(i["source_name"] for i in group_items))
        t1_items = [i for i in group_items if i["source_tier"] == 1]
        best     = (t1_items or group_items)[-1]
        clusters.append({
            "id":            0,
            "headline":      best["title"],
            "sources":       sources,
            "items":         group_items,
            "published_dts": [i["published_dt"] for i in group_items],
            "best_url":      best["url"],
        })

    clusters.sort(key=lambda c: max(c["published_dts"]), reverse=True)
    for i, c in enumerate(clusters, 1):
        c["id"] = i

    log.info(f"Formed {len(clusters)} clusters from {len(items)} items")
    return clusters

# ── Tag matching ───────────────────────────────────────────────────────────────
def match_cluster_tags(cluster: dict, priority_tags: dict, learnings: dict) -> List[dict]:
    all_text = " ".join(i["title"].lower() for i in cluster["items"])
    boosts   = learnings.get("tag_boosts", {})
    matched  = []
    for tag, stats in priority_tags.items():
        pattern = r'(?<![a-z0-9])' + re.escape(tag) + r'(?![a-z0-9])'
        if re.search(pattern, all_text):
            matched.append({
                "tag":   tag,
                "sa":    stats["sa"],
                "boost": boosts.get(tag, 0),
            })
    matched.sort(key=lambda x: x["sa"], reverse=True)
    return matched[:5]

# ── Recently posted ────────────────────────────────────────────────────────────
def load_recently_posted(learnings: dict) -> List[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENTLY_POSTED_HRS)
    return [
        s for s in learnings.get("recently_posted", [])
        if datetime.fromisoformat(s["posted_at"]) > cutoff
    ]

def record_posted_story(
    learnings: dict,
    headline: str,
    tag: str,
    urls: Optional[List[str]] = None,
    article_titles: Optional[List[str]] = None,
):
    posted = learnings.setdefault("recently_posted", [])
    posted.append({
        "headline":       headline,
        "tag":            tag.lower().strip(),
        "posted_at":      datetime.now(timezone.utc).isoformat(),
        "urls":           urls or [],
        "article_titles": article_titles or [],
    })
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    learnings["recently_posted"] = [s for s in posted if s["posted_at"] > cutoff]

# ── Article cache ──────────────────────────────────────────────────────────────
def load_article_cache(learnings: dict) -> List[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS)).isoformat()
    cache  = learnings.get("article_cache", [])
    fresh  = [a for a in cache if a.get("published_dt", "") > cutoff]
    log.info(f"Article cache: {len(fresh)} items from last {CACHE_HOURS}h")
    return fresh

def update_article_cache(learnings: dict, new_items: List[dict]):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS)).isoformat()
    existing = [a for a in learnings.get("article_cache", []) if a.get("published_dt", "") > cutoff]
    existing_guids = {a["guid"] for a in existing}
    to_add = [
        {
            "guid":         item["guid"],
            "title":        item["title"],
            "url":          item["url"],
            "published_dt": item["published_dt"].isoformat(),
            "source_name":  item["source_name"],
            "source_tier":  item["source_tier"],
        }
        for item in new_items
        if item["guid"] not in existing_guids
    ]
    learnings["article_cache"] = existing + to_add
    log.info(f"Cache updated: {len(existing)} existing + {len(to_add)} new = {len(learnings['article_cache'])} total")

def restore_cached_items(cached: List[dict]) -> List[dict]:
    restored = []
    for a in cached:
        try:
            restored.append({
                **a,
                "published_dt": datetime.fromisoformat(a["published_dt"]),
                "summary":      "",
                "_from_cache":  True,
            })
        except Exception:
            pass
    return restored

# ── Claude API ─────────────────────────────────────────────────────────────────
def _call_claude(prompt: str, max_tokens: int) -> Optional[str]:
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    retry_wait = _CLAUDE_CFG["retry_wait_seconds"]
    max_retries = _CLAUDE_CFG["max_retries"]
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=body, timeout=120,
            )
            if resp.status_code == 429:
                wait = retry_wait * (attempt + 1)
                log.warning(f"Claude 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["content"][0]["text"].strip()
        except Exception as e:
            log.warning(f"Claude call failed (attempt {attempt+1}/{max_retries+1}): {e}")
            if attempt < max_retries:
                time.sleep(retry_wait)
    return None

# ── Claude Call 1: merge + suppress ───────────────────────────────────────────
def claude_find_merges(clusters: list, recently_posted: List[dict]) -> Tuple[dict, set, set]:
    if len(clusters) <= 1:
        return {}, set(), set()

    lines = []
    for c in clusters:
        local_id = c.get("_local_id", c["id"])
        sorted_items = sorted(c["items"], key=lambda x: x["published_dt"])
        headlines = " | ".join(it["title"][:70] for it in sorted_items[:3])
        lines.append(f"[{local_id}] {headlines}")

    # URL-based pre-suppression: any cluster sharing a URL with a recently posted story is a dupe
    posted_urls: set = set()
    for s in recently_posted:
        posted_urls.update(s.get("urls", []))

    url_dupes: set = set()
    if posted_urls:
        for c in clusters:
            local_id = c.get("_local_id", c["id"])
            cluster_urls = {it["url"] for it in c["items"] if it.get("url")}
            if cluster_urls & posted_urls:
                url_dupes.add(local_id)

    pre_suppressed = url_dupes
    # Remove URL dupes before sending to Claude
    clusters = [c for c in clusters if c.get("_local_id", c["id"]) not in pre_suppressed]
    if url_dupes:
        log.info(f"URL-based pre-suppression: {len(url_dupes)} dupe clusters removed before Claude call 1")

    posted_section = ""
    if recently_posted:
        posted_lines = []
        # Most-recent 40 stories; article_titles give Claude richer context to
        # recognise the same event even when new articles phrase it differently
        for s in recently_posted[-40:]:
            extra = ""
            if s.get("article_titles"):
                samples = " | ".join(t[:70] for t in s["article_titles"][:3])
                extra = f"\n      articles: {samples}"
            posted_lines.append(f"  - {s['headline'][:100]}{extra}")
        posted_section = (
            f"RECENTLY POSTED STORIES (last {RECENTLY_POSTED_HRS}h — "
            f"flag as DUPE any cluster covering the same event, even if worded differently):\n"
            + "\n".join(posted_lines)
            + "\n\n"
        )

    prompt = f"""You are reviewing news article clusters for a movie/TV website.

You have TWO jobs:

JOB 1 — MERGES: Identify clusters that cover the SAME story and should be merged.
Same story = same news event, same film/show/person, even if worded very differently.
Different story = same franchise/subject but genuinely different news (e.g. a trailer drop vs a casting interview are different events even for the same film).

SAME EVENT RULE: News outlets routinely cover identical announcements with completely different headlines. These are ALWAYS the same event — merge them:
- "Spartacus spinoff axed after one season" + "House of Ashur cancelled at Starz" = SAME
- "X renewed for season 3" + "X coming back for another run" = SAME
- "Actor joins cast of Y" + "Y adds Actor in new role" = SAME
- "Z trailer drops" + "First look at Z released" = SAME
If two clusters are clearly about the same show/film and the same type of news (cancellation, renewal, casting, trailer), merge them regardless of phrasing.

Do NOT merge clusters about genuinely different projects even if they share keywords like "Netflix", "trailer", "animated", or studio names. Only merge when it's clearly the same specific event.

SPLIT RULE: If a single cluster contains articles about two or more genuinely different news events OR different film/TV titles, flag it under SPLITS. Do not blend them into one headline.
Key signal: if you cannot write a single honest headline that covers all the articles without naming two different movies or shows, it needs splitting.
Watch for "bridge" articles — comparison pieces, listicles, or "anticipated films" roundups that mention multiple titles. These cause Python clustering to merge unrelated stories. The bridge article does not make those stories the same event.
Example: cluster has [OUAT spin-off release date] + [The Odyssey cast news] + [Odyssey vs Troy comparison] → SPLIT, these are two different films.

JOB 1b — SPLITS: Flag any cluster whose articles are about genuinely different news events or different primary subjects.

JOB 2 — DUPES: Identify clusters that cover the SAME NEWS EVENT as a recently posted story.
A dupe is when we already posted about this specific event. It is NOT a dupe if it's a related but different event about the same subject.
Also flag as DUPE if a cluster is a "full cast roundup" or summary article that covers the same ground as a recently posted specific casting story — even if framed differently.
Example of DUPE: we posted "The Bear drops surprise episode" → new cluster "Hulu surprises fans with Bear flashback" = same event, flag as dupe.
Example of NOT A DUPE: we posted "The Odyssey trailer drops" → new cluster "Nolan confirms Tom Holland's character details" = different event, do not flag.
{posted_section}CLUSTERS:
{chr(10).join(lines)}

OUTPUT FORMAT — respond with three sections:

MERGES:
[cluster IDs separated by commas, one group per line. Write NONE if no merges.]

DUPES:
[single cluster IDs to suppress, one per line. Write NONE if no dupes.]

SPLITS:
[single cluster IDs whose articles cover different news events, one per line. Write NONE if no splits needed.]

Example:
MERGES:
3,7,12
15,22

DUPES:
8
19

SPLITS:
4
21

Do not explain. Just the numbers in the correct sections."""

    result = _call_claude(prompt, max_tokens=_CLAUDE_CFG["call1_max_tokens"])
    if not result:
        return {}, pre_suppressed, set()

    merges: dict = {}
    dupes:  set  = set()
    current = None

    merge_section = ""
    dupe_section  = ""
    split_section = ""
    for line in result.strip().splitlines():
        ls = line.strip()
        if ls.upper().startswith("MERGES"):
            current = "merge"
        elif ls.upper().startswith("DUPES"):
            current = "dupe"
        elif ls.upper().startswith("SPLITS"):
            current = "split"
        elif current == "merge":
            merge_section += ls + "\n"
        elif current == "dupe":
            dupe_section += ls + "\n"
        elif current == "split":
            split_section += ls + "\n"

    for line in merge_section.strip().splitlines():
        if line.strip().upper() == "NONE":
            continue
        ids = []
        for part in re.split(r'[,\s]+', line.strip()):
            try:
                ids.append(int(part))
            except ValueError:
                pass
        if len(ids) >= 2:
            merges[ids[0]] = ids

    for line in dupe_section.strip().splitlines():
        if line.strip().upper() == "NONE":
            continue
        try:
            dupes.add(int(line.strip()))
        except ValueError:
            pass

    splits: set = set()
    for line in split_section.strip().splitlines():
        if line.strip().upper() == "NONE":
            continue
        try:
            splits.add(int(line.strip()))
        except ValueError:
            pass

    if dupes:
        log.info(f"Claude flagged {len(dupes)} clusters as dupes: {dupes}")
    if splits:
        log.info(f"Claude flagged {len(splits)} clusters for splitting (mixed stories): {splits}")
    # Merge URL-pre-suppressed IDs back so the caller drops them from the full cluster list
    dupes = dupes | pre_suppressed
    return merges, dupes, splits


def apply_merges(clusters: list, merges: dict) -> list:
    if not merges:
        return clusters

    cluster_by_local = {c.get("_local_id", c["id"]): c for c in clusters}
    absorbed = {gid for group_ids in merges.values() for gid in group_ids[1:]}

    merged = []
    for c in clusters:
        local_id = c.get("_local_id", c["id"])
        if local_id in absorbed:
            continue
        if local_id in merges:
            group_ids  = merges[local_id]
            all_items  = []
            all_sources = []
            for gid in group_ids:
                gc = cluster_by_local.get(gid)
                if gc:
                    all_items.extend(gc["items"])
                    all_sources.extend(gc["sources"])
            all_items.sort(key=lambda x: x["published_dt"])
            sources = list(dict.fromkeys(all_sources))
            t1 = [i for i in all_items if i["source_tier"] == 1]
            best = (t1 or all_items)[-1]
            merged.append({
                **c,
                "items":         all_items,
                "sources":       sources,
                "published_dts": [i["published_dt"] for i in all_items],
                "best_url":      best["url"],
                "headline":      best["title"],
                "_merged_from":  group_ids,
            })
            log.info(f"Merged clusters {group_ids} → [{local_id}] {best['title'][:60]}")
        else:
            merged.append(c)

    return merged

# ── Claude Call 2: editorial assessment ───────────────────────────────────────
def claude_assess_clusters(clusters: list, priority_tags: dict, learnings: dict) -> List[dict]:
    if not clusters:
        return []

    CHUNK_SIZE = 40
    if len(clusters) > CHUNK_SIZE:
        results = []
        for i in range(0, len(clusters), CHUNK_SIZE):
            chunk = clusters[i:i + CHUNK_SIZE]
            for j, c in enumerate(chunk, 1):
                c["_local_id"] = j
            log.info(f"  Assessing chunk {i // CHUNK_SIZE + 1} ({len(chunk)} clusters)...")
            results.extend(claude_assess_clusters(chunk, priority_tags, learnings))
        return results

    cluster_blocks = []
    for seq, c in enumerate(clusters, 1):
        # Label blocks 1..N by position so they align with _parse_assessments,
        # which maps Claude's "CLUSTER n" reply back to clusters[n-1] positionally.
        # Using _local_id/id here breaks after merge/dupe filtering leaves gaps.
        n_t1 = sum(1 for i in c["items"] if i["source_tier"] == 1)
        n_t2 = sum(1 for i in c["items"] if i["source_tier"] == 2)
        sorted_items = sorted(c["items"], key=lambda x: x["published_dt"])
        articles = []
        for it in sorted_items:
            age  = int((datetime.now(timezone.utc) - it["published_dt"]).total_seconds() / 60)
            tier = "T1" if it["source_tier"] == 1 else "T2"
            articles.append(f"  [{tier}] {it['source_name']:<22} {age:>3}m ago | {it['title']}")
        matched = match_cluster_tags(c, priority_tags, learnings)
        if matched:
            tag_lines = [f"    - {m['tag']} (S/A: {int(m['sa']):,})" for m in matched]
            tag_context = "MW Sheet Tags matched:\n" + "\n".join(tag_lines) + "\n"
        else:
            tag_context = "MW Sheet Tags matched: none\n"
        cluster_blocks.append(
            f"CLUSTER {seq} | T1: {n_t1}  T2: {n_t2}  Total: {n_t1 + n_t2}\n"
            f"Sources: {', '.join(c['sources'])}\n"
            f"{tag_context}"
            + "\n".join(articles)
        )

    # Build editorial context from learnings
    editorial_notes = learnings.get("editorial_notes", [])
    editorial_section = ""
    if editorial_notes:
        editorial_section = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "TEAM EDITORIAL PREFERENCES (learned from team feedback — apply to ALL tiers):\n"
            + "\n".join(f"  • {note}" for note in editorial_notes)
            + "\n"
        )

    boosts = learnings.get("tag_boosts", {})
    boost_lines = [(tag, score) for tag, score in boosts.items() if abs(score) >= 1.0]
    boost_section = ""
    if boost_lines:
        boost_lines.sort(key=lambda x: x[1], reverse=True)
        pos = [f"{tag} ({score:+.1f})" for tag, score in boost_lines if score > 0]
        neg = [f"{tag} ({score:+.1f})" for tag, score in boost_lines if score < 0]
        parts = []
        if pos:
            parts.append(f"  Team has reacted positively to: {', '.join(pos)}")
        if neg:
            parts.append(f"  Team has reacted negatively to: {', '.join(neg)}")
        if parts:
            boost_section = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "TAG FEEDBACK SIGNALS (factor into tier and relevance scores):\n"
                + "\n".join(parts) + "\n"
            )

    prompt = f"""You are the editorial brain of Legolas, a news scout for MovieWeb (MW) — a mainstream US entertainment website covering movies, TV, streaming, and pop culture.

Your default stance is SKEPTIC. Most clusters should be skipped. You are looking for the rare story that is genuinely worth interrupting a reader's day.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY PRE-CHECK — for every cluster, before assigning any tier except skip, you MUST be able to answer YES to ALL of the following:

  1. NAMED? Does the story concern a SPECIFIC named show, film, franchise, or person?
     → "a new Netflix series", "an animated superhero show", "a streaming drama" = NO → skip
     → "The Boys", "Avengers: Doomsday", "Tom Hanks" = YES

  2. EVENT? Did a concrete, confirmed news event actually happen?
     → "taking over globally", "going viral", "fans love it" = NO → skip
     → "cancelled", "trailer dropped", "cast confirmed", "box office record broken" = YES
     → Speculation, analysis, op-ed, "could this mean", "fans believe" = NO → skip

  3. NEW? Is this news that broke recently, not evergreen background information?
     → Retrospectives, legacy pieces, anniversary features = NO → skip

  4. INTERESTING? If you saw this headline on your phone right now, would you actually click it?
     → Not "is this technically news" — is this genuinely compelling to a movie/TV fan?
     → These pass checks 1-3 but are NOT interesting — skip them:
        • A franchise prop/costume donated to a museum ("Eleven's dress goes to Smithsonian")
        • A cast member's charity work, personal milestone, or lifestyle news with no production hook
        • A show being "beloved" or "iconic" with no new development attached
        • A minor crew hire, location scout, or production logistics update
        • A celebrity's personal life disclosure (coming out, marriage, divorce, health) unless
          it DIRECTLY impacts a specific role — e.g. they're leaving a show because of it
        • Festival reviews of films with no existing MW audience (arthouse, Cannes competition
          films, non-English language films without major US release)

If ANY of these fail → TIER: skip, MW_RELEVANCE: 1-3.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TIER rules (only apply after pre-check passes):
- trending: 4+ independent publishers covering this story. Source tier does NOT matter — broad independent coverage is the bar. NOTE: outlets owned by the same parent company (Variety, Deadline, The Hollywood Reporter and Rolling Stone are all Penske/PMC) count as roughly HALF a source between them — three PMC mastheads running the same story is one company talking to itself, not a broad trend. Look for independent corroboration before calling trending.
- proven_topic: Real breaking news about a subject matching "MW Sheet Tags matched". Generic platform/genre tags (netflix, streaming, sci-fi, thriller, superhero, action) alone are NEVER enough — the tag must be a specific franchise, title, or person.

  proven_topic REQUIRES a concrete news event. Valid: casting confirmed, cancellation announced, renewal confirmed, trailer dropped, box office milestone, controversy broke, exclusive production reveal.
  NOT valid: episode preview clip, aftershow interview, cast discussing legacy, "what to expect" guide, behind-the-scenes feature, promotional interview timed to an airing, fan Q&A, character analysis, show "trending" or "popular".

  Cast interviews: only proven_topic if the cast member CONFIRMED a specific news hook (return, departure, plot reveal). "Talks about their experience" = skip.
  Reviews: only allow if it's a major release (blockbuster franchise, A-list cast, wide release) AND multiple T1 sources are covering it.

- legolas_special: Use VERY SPARINGLY. This is your editorial override — a story that fails trending/proven_topic but is so good you'd flag it anyway. It is a pure QUALITY judgment: the source tier and how many outlets ran it DO NOT matter. A single great scoop from one outlet belongs here; a dull story carried by five trades does NOT. Must clear all 4 pre-check questions, be squarely for the MW entertainment audience (NOT politics, sports, business/industry deals, product/shopping, fashion, music tours, theater), and score MW_RELEVANCE ≥ {LEGOLAS_SPECIAL_MIN}. If you're not genuinely excited, skip. Skipping everything in a batch is fine — better than posting noise.

- skip: Everything else. When in doubt, skip. A missed story is better than a bad post.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MovieWeb CARES: films, TV, streaming (Netflix/Disney+/HBO Max/Amazon), casting, cancellations, renewals, box office, Oscars/Emmys, major franchise updates (MCU, Star Wars, DC, Yellowstone, etc), major celebrity entertainment news.

MovieWeb does NOT care: gaming hardware, politics, UK/Australian soaps, niche festival films, financial/earnings reports, music tours, fashion events, Broadway/theater (any kind — even A-list), product reviews, travel, science, sports.

ALWAYS SKIP regardless of source count:
- Lists, rankings, "top N", "ranked", "best of"
- "What to watch", streaming calendars, "leaving this month", scheduling guides
- Evergreen, retrospectives, "whatever happened to", "X years later", anniversary features
- Podcast/audio content, "LISTEN:"
- Fan theories, "could this mean", "fans believe", speculation — unless confirmed news is behind it
- Music acts at award shows, concert announcements, album releases (unless artist is primarily a screen star)
- Celebrities running for office or receiving political endorsements
- Celebrity personal life disclosures — coming out, marriage, divorce, health, relationships — UNLESS the person explicitly confirms it directly impacts a specific role or production
- "Trending on Netflix/globally", "taking over streaming", "fans are loving" — NOT a news event
- Fashion/Met Gala as standalone stories
- Broadway/theater of any kind
- Network upfront/slate announcements unless they contain a SPECIFIC major surprise
- Stories about a show being "popular", "beloved", or "iconic" without a specific new development
- Cannes/Venice/TIFF/Sundance/Berlin reviews unless the film is: a franchise sequel, an A-list English-language tentpole with a confirmed wide US release, or a film already generating major MW-level buzz. Arthouse competition films, foreign-language films, and debut features = skip regardless of critical praise or source count

MW_RELEVANCE (only score after pre-check passes — if pre-check fails, score 1-3):
10 = every MW reader needs this right now (Avengers casting, Oscar winner, major cancellation)
8-9 = strong story (franchise update, A-list news, box office record)
6-7 = solid MW story (mid-tier casting, renewal, specific streaming news)
4-5 = niche — do not post
1-3 = skip (pre-check failed, wrong audience, evergreen)

{editorial_section}{boost_section}{chr(10).join(cluster_blocks)}

For EACH cluster respond with EXACTLY this format:

CLUSTER [N]
TIER: trending/proven_topic/legolas_special/skip
MW_RELEVANCE: [1-10]
HEADLINE: [punchy MW headline — MUST include the specific show/film/franchise name]
ANGLE: [one sentence hook — what specifically happened?]
TAG: [specific show/film/franchise name, or blank if skip]
SHEET_TAG_USED: [exact tag from MW Sheet Tags matched that validates proven_topic — must be a SPECIFIC named franchise, title, or person. NEVER a common word or fragment (e.g. "it", "you", "men", "hope", "v"), a stray symbol, or a generic platform/genre. If the only matched tags are generic words, leave this BLANK and do not call proven_topic. This field is authoritative — Python validates the proven_topic against exactly the tag you name here.]
NOTE: [one sentence — which pre-check passed/failed, or why posting]

Post threshold: MW_RELEVANCE >= {MW_RELEVANCE_MIN} and tier is not skip.
legolas_special requires MW_RELEVANCE >= {LEGOLAS_SPECIAL_MIN}.
"""

    result = _call_claude(prompt, max_tokens=_CLAUDE_CFG["call2_max_tokens"])
    if not result:
        log.warning("Claude assessment returned nothing — using fallback")
        return [_fallback_assessment(c) for c in clusters]

    return _parse_assessments(result, len(clusters), clusters)


_FIELD_LABELS = {"TIER", "MW_RELEVANCE", "HEADLINE", "ANGLE", "TAG", "SHEET_TAG_USED", "NOTE", "CLUSTER"}

def _parse_assessments(text: str, expected: int, clusters: list) -> List[dict]:
    def get_field(block, field):
        m = re.search(rf'^{field}:\s*(.+)$', block, re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def clean(value: str) -> str:
        """Return empty string if the value looks like a field label — means the parse grabbed the wrong line."""
        if not value:
            return ""
        first_token = value.split(":")[0].strip().upper()
        return "" if first_token in _FIELD_LABELS else value

    parts = re.split(r'\n?CLUSTER\s+(\d+)\n', text)
    cluster_texts = {}
    i = 1
    while i < len(parts) - 1:
        try:
            cluster_texts[int(parts[i])] = parts[i + 1]
        except (ValueError, IndexError):
            pass
        i += 2

    results = []
    for n in range(1, expected + 1):
        block = cluster_texts.get(n, "")
        if not block:
            log.warning(f"No assessment for cluster {n} — using fallback")
            results.append(_fallback_assessment(clusters[n - 1]))
            continue
        try:
            mw = int(re.search(r'\d+', get_field(block, "MW_RELEVANCE") or "0").group())
        except (AttributeError, ValueError):
            mw = 0
        tier = get_field(block, "TIER").lower()
        if tier not in ("trending", "proven_topic", "legolas_special"):
            tier = "skip"

        _raw_headline = clean(get_field(block, "HEADLINE"))
        headline = (_raw_headline if _raw_headline and _raw_headline != "—" else None) or clusters[n - 1]["headline"]
        angle    = clean(get_field(block, "ANGLE"))
        tag      = clean(get_field(block, "TAG"))

        if not clean(get_field(block, "HEADLINE")):
            log.warning(f"Cluster {n}: blank/malformed HEADLINE from Claude — using raw cluster headline")

        results.append({
            "tier":           tier,
            "mw_relevance":   mw,
            "headline":       headline,
            "angle":          angle,
            "tag":            tag,
            "sheet_tag_used": get_field(block, "SHEET_TAG_USED").lower(),
            "note":           get_field(block, "NOTE"),
        })

    return results


def _fallback_assessment(cluster: dict) -> dict:
    # Degraded path when Claude is unavailable. Phase 1: trend on broad coverage
    # (4+ distinct publishers, any tier), not T1 count.
    n_pubs = len({i["source_name"] for i in cluster["items"]})
    return {
        "tier":           "trending" if n_pubs >= 4 else "skip",
        "mw_relevance":   7 if n_pubs >= 4 else 3,
        "headline":       cluster["headline"],
        "angle":          "",
        "tag":            "",
        "sheet_tag_used": "",
        "note":           f"fallback — Claude unavailable ({n_pubs} publishers)",
    }

# ── Tier enforcement (Python overrides Claude's suggestions) ───────────────────
def enforce_tier(story: dict, cluster: dict, priority_tags: dict, learnings: dict) -> Tuple[str, dict]:
    """
    Enforces mechanical tier rules per PRD corrections Step 7b.
    Returns (final_tier, matched_tags_by_name).
    """
    tier = story["tier"]
    mw   = story["mw_relevance"]

    matched_tags    = match_cluster_tags(cluster, priority_tags, learnings)
    matched_by_name = {m["tag"].lower(): m for m in matched_tags}

    # Count distinct publishers (any tier), applying corporate half-weighting
    # (PMC outlets count 0.5) so one parent can't trigger trending on its own.
    # Phase 1: feed tier no longer privileges trending — broad independent
    # coverage is the bar, not "which tier covered it." Claude judges quality.
    weighted_total = 0.0
    for s in cluster["sources"]:
        w = 0.5 if s in _HALF_WEIGHT_SOURCE else 1.0
        weighted_total += w

    # Validate proven_topic. Prefer the tag CLAUDE chose (SHEET_TAG_USED) — Claude
    # is the editorial brain and decides which topic is real; Python only confirms
    # the tag exists in the sheet, isn't a generic platform/genre, and clears S/A.
    # The mechanical match is a backstop for when Claude leaves SHEET_TAG_USED blank.
    if tier == "proven_topic":
        claude_tag = (story.get("sheet_tag_used") or "").strip().lower()
        valid_match = None
        if claude_tag and claude_tag not in _GENERIC_TAGS and not _is_junk_tag(claude_tag):
            valid_match = matched_by_name.get(claude_tag) or {
                "tag": claude_tag,
                "sa":  priority_tags.get(claude_tag, {}).get("sa", 0),
                "boost": 0,
            }
            if valid_match["sa"] < TAG_SA_MIN:
                valid_match = None
        if valid_match is None:  # backstop: mechanical match
            valid_match = next(
                (m for m in matched_tags if m["tag"] not in _GENERIC_TAGS and m["sa"] >= TAG_SA_MIN),
                None,
            )
        if not valid_match:
            log.info(f"Demote proven_topic→legolas_special (no valid sheet match): {story['headline'][:60]}")
            tier = "legolas_special"
        else:
            story["_validated_sheet_tag"] = valid_match

    # Validate trending: 4+ distinct weighted publishers, any tier (PMC outlets
    # count 0.5 each, so Deadline+Variety+THR alone = 1.5, nowhere near 4).
    if tier == "trending" and weighted_total < 4:
        log.info(f"Demote trending→legolas_special (total={weighted_total:g} wt publishers): {story['headline'][:60]}")
        tier = "legolas_special"

    # Proven-topic upgrade: a story Claude already wanted to post (legolas_special)
    # that has a strong, specific sheet tag is really a proven topic. We do NOT
    # upgrade from skip — if Claude judged it a skip, that editorial call stands.
    if tier == "legolas_special" and mw >= MW_RELEVANCE_MIN:
        for m in matched_tags:
            if m["tag"] not in _GENERIC_TAGS and m["sa"] >= TAG_SA_MIN:
                log.info(f"Upgrade legolas_special→proven_topic (sheet match: {m['tag']}, {int(m['sa']):,} S/A): {story['headline'][:60]}")
                tier = "proven_topic"
                story["_validated_sheet_tag"] = m
                break

    # legolas_special is Claude's editorial override — a pure quality judgment.
    # Source tier/count does NOT gate it: a genuinely good single-source story
    # should post, and a weak multi-trade story should not. Whether a story is
    # "good enough to flag" is decided by Claude (Call 2) and Aragorn (Call 3),
    # not by counting publishers. Python only enforces the hard MW floor.
    if tier == "legolas_special" and mw < LEGOLAS_SPECIAL_MIN:
        log.info(f"Demote legolas_special→skip (mw={mw} < {LEGOLAS_SPECIAL_MIN}): {story['headline'][:60]}")
        tier = "skip"

    # Global MW gate
    if mw < MW_RELEVANCE_MIN:
        tier = "skip"

    return tier, matched_by_name

# ── Slack feedback processing ──────────────────────────────────────────────────
def _interpret_reply_with_claude(reply_text: str, story_headline: str, story_tag: str) -> Optional[dict]:
    prompt = f"""You are reviewing editorial feedback on a news story posted by Legolas, an automated entertainment news scout for MovieWeb.

Story headline: {story_headline}
Story tag: {story_tag}
Editor reply: {reply_text}

Interpret this feedback and return a JSON object with exactly these fields:
{{
  "action": "boost" | "penalize" | "note" | "ignore",
  "tag": "{story_tag}",
  "delta": 0.0,
  "reason": "one sentence explanation",
  "story_type_note": "optional: if the feedback is about the TYPE of story note it here, else null"
}}

Action guidelines:
- "boost": editor thinks this was a great pick, more like this (+0.5 to +1.5 delta)
- "penalize": editor thinks this shouldn't have posted (-0.5 to -1.5 delta)
- "note": feedback is about process/rules, not the tag specifically (delta: 0)
- "ignore": reaction/comment not relevant to editorial quality

Be conservative with deltas. Most feedback should be ±0.5 to ±1.0.
Respond with valid JSON only. No explanation outside the JSON."""

    result = _call_claude(prompt, max_tokens=200)
    if not result:
        return None
    try:
        clean = re.sub(r'```json|```', '', result).strip()
        return json.loads(clean)
    except Exception:
        log.warning(f"Could not parse Claude feedback interpretation: {result[:100]}")
        return None


def _log_learning(learnings: dict, entry: dict):
    log_entries = learnings.setdefault("learnings_log", [])
    log_entries.append({**entry, "logged_at": datetime.now(timezone.utc).isoformat()})
    learnings["learnings_log"] = log_entries[-500:]


def _apply_boost(learnings: dict, text: str, priority_tags: dict, delta: float):
    text_lower = text.lower()
    for tag in priority_tags:
        if tag in text_lower:
            boosts = learnings.setdefault("tag_boosts", {})
            boosts[tag] = round(boosts.get(tag, 0) + delta, 2)


def _extract_tier_from_message(text: str) -> str:
    if "Big Trend" in text or "📈" in text:
        return "trending"
    if "MW Proven Topic" in text or "🎯" in text:
        return "proven_topic"
    if "Legolas Special" in text or "⭐" in text:
        return "legolas_special"
    return ""


def synthesize_editorial_notes(learnings: dict) -> None:
    """
    Reads recent feedback log entries and synthesizes natural-language editorial notes
    via Claude. Notes are stored in learnings["editorial_notes"] and injected into
    Claude Call 2 to shape judgment across all tiers.

    Throttled: only re-runs when new feedback has arrived since last synthesis,
    and at most once every 6 hours.
    """
    log_entries = learnings.get("learnings_log", [])
    if not log_entries:
        return

    last_count   = learnings.get("_editorial_notes_log_count", 0)
    last_updated = learnings.get("editorial_notes_updated_at")

    if len(log_entries) == last_count:
        log.info("No new feedback since last editorial synthesis — skipping")
        return

    if last_updated:
        age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last_updated)).total_seconds() / 3600
        if age_hours < 6:
            log.info(f"Editorial synthesis ran {age_hours:.1f}h ago — throttled")
            return

    feedback_lines = []
    for entry in log_entries[-60:]:
        etype = entry.get("type", "")
        if etype == "emoji_reaction":
            reaction  = entry.get("reaction", "")
            story     = entry.get("story", "")[:120]
            tier_hint = entry.get("tier", "")
            label     = f"[{tier_hint}] " if tier_hint else ""
            feedback_lines.append(f"REACTION {reaction} on {label}{story}")
        elif etype == "reply_comment":
            headline  = entry.get("story_headline", "")
            tier_hint = entry.get("tier", "")
            reply     = entry.get("reply_text", "")[:150]
            action    = entry.get("interpreted_action", "")
            note      = entry.get("story_type_note") or ""
            label     = f"[{tier_hint}] " if tier_hint else ""
            feedback_lines.append(
                f"REPLY ({action}) on {label}'{headline}': \"{reply}\""
                + (f" [story type note: {note}]" if note else "")
            )

    if not feedback_lines:
        return

    recently_posted = learnings.get("recently_posted", [])[-20:]
    posted_lines    = [f"  - [{s.get('tag', '')}] {s['headline']}" for s in recently_posted]
    posted_section  = ("Recent stories posted by Legolas:\n" + "\n".join(posted_lines) + "\n\n") if posted_lines else ""

    prompt = f"""You are the editorial director of Legolas, a news scout for MovieWeb (MW).

Below is a log of editorial feedback the team has given on recent stories — emoji reactions (👍/👎) and thread replies. Each reaction shows the tier the story was posted as: [trending], [proven_topic], or [legolas_special].

{posted_section}FEEDBACK LOG:
{chr(10).join(feedback_lines)}

Synthesize this into 3–8 clear, actionable editorial notes that will guide future story selection. These notes will be shown to an AI assessor for EVERY future story across ALL tiers.

Rules for writing notes:
- One sentence per note
- Be specific — name the show/franchise/story type where the feedback is clear enough
- Distinguish nuance: "we don't want GoT at all" vs "we want GoT but only major news, not minor updates"
- Capture scale of story: a thumbs-down on a minor story about a popular franchise ≠ avoid that franchise
- If a 👎 was on a [trending] story vs a [legolas_special], that's different signal — note it
- Only write notes where the feedback is unambiguous enough to act on
- Do NOT write notes for ambiguous or contradictory feedback

Output notes only — no bullet points, no numbers, no preamble."""

    result = _call_claude(prompt, max_tokens=500)
    if not result:
        log.warning("Editorial synthesis returned nothing")
        return

    notes = [line.strip() for line in result.strip().splitlines() if line.strip()]
    if notes:
        learnings["editorial_notes"]             = notes
        learnings["editorial_notes_updated_at"]  = datetime.now(timezone.utc).isoformat()
        learnings["_editorial_notes_log_count"]  = len(log_entries)
        log.info(f"Synthesized {len(notes)} editorial notes from {len(feedback_lines)} feedback entries")
        for n in notes:
            log.info(f"  Editorial note: {n}")


def process_feedback(learnings: dict, priority_tags: dict, svc=None):
    try:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        resp = requests.get(
            "https://slack.com/api/conversations.history",
            headers=headers,
            params={"channel": SLACK_CHANNEL_ID, "limit": 50},
            timeout=15,
        )
        msgs = resp.json().get("messages", [])
        processed_r  = set(learnings.get("processed_reactions", []))
        processed_re = set(learnings.get("processed_replies", []))

        for msg in msgs:
            ts   = msg.get("ts", "")
            text = msg.get("text", "")

            msg_tier = _extract_tier_from_message(text)
            for reaction in msg.get("reactions", []):
                reaction_id = f"{ts}:{reaction['name']}"
                if reaction_id in processed_r:
                    continue
                if reaction["name"] in ("thumbsup", "+1"):
                    _apply_boost(learnings, text, priority_tags, +1.0)
                    _log_learning(learnings, {"type": "emoji_reaction", "reaction": "👍", "story": text[:100], "tier": msg_tier, "delta": +1.0, "reason": "thumbs up"})
                elif reaction["name"] in ("thumbsdown", "-1"):
                    _apply_boost(learnings, text, priority_tags, -1.0)
                    _log_learning(learnings, {"type": "emoji_reaction", "reaction": "👎", "story": text[:100], "tier": msg_tier, "delta": -1.0, "reason": "thumbs down"})
                processed_r.add(reaction_id)

            if msg.get("reply_count", 0) > 0 and ts not in processed_re:
                try:
                    replies_resp = requests.get(
                        "https://slack.com/api/conversations.replies",
                        headers=headers,
                        params={"channel": SLACK_CHANNEL_ID, "ts": ts},
                        timeout=15,
                    ).json()

                    story_headline = ""
                    story_tag      = ""
                    headline_match = re.search(r'\*(.*?)\*', text)
                    tag_match      = re.search(r'Pri Tag:\s*([^\n_(]+)', text)
                    if headline_match:
                        story_headline = headline_match.group(1).strip()
                    if tag_match:
                        story_tag = tag_match.group(1).strip()

                    for reply in replies_resp.get("messages", [])[1:]:
                        reply_id  = f"{ts}:{reply.get('ts','')}"
                        if reply_id in processed_re:
                            continue
                        reply_text = reply.get("text", "").strip()
                        if not reply_text or len(reply_text) < 3:
                            continue
                        action = _interpret_reply_with_claude(reply_text, story_headline, story_tag)
                        if action and action.get("action") != "ignore":
                            tag   = action.get("tag", story_tag).lower().strip()
                            delta = float(action.get("delta", 0))
                            if delta != 0 and tag:
                                boosts = learnings.setdefault("tag_boosts", {})
                                boosts[tag] = round(boosts.get(tag, 0) + delta, 2)
                                log.info(f"Reply feedback: {action['action']} tag '{tag}' by {delta}: {action.get('reason','')}")
                            _log_learning(learnings, {
                                "type":               "reply_comment",
                                "reply_text":         reply_text[:200],
                                "story_headline":     story_headline,
                                "story_tag":          story_tag,
                                "tier":               msg_tier,
                                "interpreted_action": action.get("action"),
                                "tag_affected":       tag,
                                "delta":              delta,
                                "reason":             action.get("reason", ""),
                                "story_type_note":    action.get("story_type_note"),
                            })
                            if svc:
                                _write_feedback_to_sheet(svc, story_headline, story_tag, reply_text, action, tag, delta)
                        processed_re.add(reply_id)
                    processed_re.add(ts)
                except Exception as e:
                    log.warning(f"Could not process replies for {ts}: {e}")

        learnings["processed_reactions"] = list(processed_r)
        learnings["processed_replies"]   = list(processed_re)
        log.info("Processed Slack feedback")
    except Exception as e:
        log.warning(f"Feedback processing failed: {e}")


def _write_feedback_to_sheet(svc, story_headline, story_tag, reply_text, action, tag, delta):
    try:
        tab = find_tab(svc, [_SHEETS_CFG["feedback_log_tab"], "FeedbackLog", "feedback_log"])
        if not tab:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": _SHEETS_CFG["feedback_log_tab"]}}}]},
            ).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{_SHEETS_CFG['feedback_log_tab']}!A1",
                valueInputOption="RAW",
                body={"values": [["timestamp", "story_headline", "story_tag", "reply_text",
                                  "interpreted_action", "tag_affected", "delta", "reason", "story_type_note"]]},
            ).execute()
            tab = _SHEETS_CFG["feedback_log_tab"]
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{tab}!A:I",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[
                datetime.now(timezone.utc).isoformat(),
                story_headline, story_tag, reply_text[:200],
                action.get("action", ""), tag, delta,
                action.get("reason", ""), action.get("story_type_note") or "",
            ]]},
        ).execute()
    except Exception as e:
        log.warning(f"Could not write to Feedback Log sheet: {e}")

# ── Aragorn: title audit + rewrite ────────────────────────────────────────────
def aragorn_audit(will_post: List[Tuple]) -> List[Tuple]:
    """
    Call 3: Aragorn audits every story Legolas approved.
    For each story: KEEP (with an optimised headline) or KILL (with a reason).
    Returns a filtered list of (cluster, assessment, tier) tuples with headlines
    potentially rewritten.
    """
    if not will_post:
        return []

    story_blocks = []
    for i, (cluster, assessment, tier) in enumerate(will_post, 1):
        n_t1 = sum(1 for it in cluster["items"] if it["source_tier"] == 1)
        articles = [f"  {it['title']}" for it in sorted(cluster["items"], key=lambda x: x["published_dt"])]
        story_blocks.append(
            f"STORY {i}\n"
            f"Tier: {tier} | T1 sources: {n_t1} | MW relevance: {assessment['mw_relevance']}/10\n"
            f"Legolas headline: {assessment['headline']}\n"
            f"Angle: {assessment.get('angle', '')}\n"
            f"Source articles:\n" + "\n".join(articles)
        )

    prompt = f"""You are Aragorn, the title editor for MovieWeb (MW) — a mainstream US entertainment website.

Legolas (the news scout) has approved the stories below. Your job is to audit each one: kill it if it has no path to performance, or keep it with the strongest possible headline.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE SINGLE MOST IMPORTANT RULE

Descriptor-led, not show-name-led.
Screen Rant leads with a show name in quotes in 0.5% of titles. MovieWeb does it 41% of the time. SR averages 7,992 sessions vs MW's 3,575 on identical stories — framing is the entire gap.

Every headline must describe the show or film for someone who has never heard of it. Sell the concept, not the brand name. Withhold enough to create a curiosity gap — the reader must need to click.

YOU MUST REWRITE THE HEADLINE if:
- It opens with a show/film title in quotes (e.g. "'Euphoria' Star..." → "Sydney Sweeney...")
- It buries a star name when one is available (lead with the star)
- It gives away the full story with nothing left to click for
- It is passive, vague, or uses "reportedly," "eyes," "addresses," "reflects on"
- It reads like a wire service headline, not a Discover-optimised title
Only leave the headline unchanged if it already passes all of the above.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

KILL if ANY of these are true:
- Uses "almost," "nearly," "tried to" (0% hit rate, 60% fail — nothing happened)
- Purely backward-looking with no forward news
- Minor announcement for a cold IP with no A-list star or major platform hook
- Celebrity quote with no concrete news attached ("reflects on," "remembers," "offers take on")
- IP in the 0% hit rate list with no overriding hook: Daredevil: Born Again, Star Wars: Maul, Man of Tomorrow, Hoppers, Sinners, Superman

These are not suggestions. If a kill signal is present, KILL.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TITLE CONSTRUCTION — apply in order:

1. Lead with the biggest name: Star > Director > Platform > Property
2. Point forward: "confirms filming" beats "remembers working on"
3. Create a curiosity gap: reader must need to click, withhold the punchline
4. Concrete achievements: numbers, rankings, records — never vague praise
5. Describe unknown properties: genre + scale + platform + pedigree
6. Discover scroll test: would you scroll past this in a feed? If yes, rewrite.

Proven mechanisms (use in priority order):
1. STAR-LED — 46% hit, 6% fail. Tier-1 names: Cavill, Statham, Cruise, Hemsworth, Ritchson, Crowe, Butler, Jackman, DiCaprio, Keanu Reeves, Taylor Swift (cultural scale), Scarlett Johansson, Sydney Sweeney. Pattern: [Star]'s [Genre Descriptor] [Forward verb]
2. X YEARS LATER — 39% hit. ONLY on household-name IPs.
3. PLATFORM-LED — 25% hit. "Netflix's…" / "HBO's…" triggers the streamer-browsing instinct.
4. MILESTONE — 22% hit. Concrete numbers and records only.
5. GENRE COMP — 20% hit. "X meets Y" stacked on a Tier 1 anchor.
6. REVELATION / CONTROVERSY — sparingly, only on culturally massive IPs.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{chr(10).join(story_blocks)}

For EACH story respond with EXACTLY this format:

STORY [N]
DECISION: KEEP/KILL
HEADLINE: [your headline — or the original if it's already strong]
MECHANISM: [which mechanism you used]
REASON: [one sentence — why you're keeping/killing, or what you changed and why]
"""

    result = _call_claude(prompt, max_tokens=1200)
    if not result:
        log.warning("Aragorn returned nothing — passing all stories through unchanged")
        return will_post

    # Parse Aragorn's response
    parts   = re.split(r'\nSTORY\s+(\d+)\n', result)
    verdicts: Dict[int, dict] = {}
    i = 1
    while i < len(parts) - 1:
        try:
            idx  = int(parts[i])
            body = parts[i + 1]
            decision  = re.search(r'^DECISION:\s*(\w+)', body, re.MULTILINE)
            headline  = re.search(r'^HEADLINE:\s*(.+)$', body, re.MULTILINE)
            mechanism = re.search(r'^MECHANISM:\s*(.+)$', body, re.MULTILINE)
            reason    = re.search(r'^REASON:\s*(.+)$', body, re.MULTILINE)
            verdicts[idx] = {
                "decision":  decision.group(1).upper() if decision else "KEEP",
                "headline":  headline.group(1).strip() if headline else "",
                "mechanism": mechanism.group(1).strip() if mechanism else "",
                "reason":    reason.group(1).strip() if reason else "",
            }
        except (ValueError, IndexError):
            pass
        i += 2

    approved = []
    for i, (cluster, assessment, tier) in enumerate(will_post, 1):
        verdict = verdicts.get(i)
        if not verdict:
            log.warning(f"Aragorn: no verdict for story {i} — passing through")
            approved.append((cluster, assessment, tier))
            continue

        if verdict["decision"] == "KILL":
            log.info(f"🗡 Aragorn KILL [{tier}]: {assessment['headline'][:60]} — {verdict['reason'][:80]}")
            continue

        original = assessment["headline"]
        if verdict["headline"] and verdict["headline"] != original:
            assessment = {**assessment, "headline": verdict["headline"]}
            log.info(f"✏️  Aragorn rewrote [{verdict['mechanism']}]: '{original[:50]}' → '{verdict['headline'][:60]}'")
        else:
            log.info(f"✅ Aragorn kept [{verdict['mechanism']}]: {assessment['headline'][:60]}")

        approved.append((cluster, assessment, tier))

    log.info(f"Aragorn: {len(approved)}/{len(will_post)} stories approved")
    return approved


# ── Slack posting ──────────────────────────────────────────────────────────────
def post_to_slack(cluster: dict, assessment: dict, tier: str) -> bool:
    tier_labels = {
        "trending":        "📈 Big Trend",
        "proven_topic":    "🎯 MW Proven Topic",
        "legolas_special": "⭐ Legolas Special Pick",
    }
    tier_label = tier_labels.get(tier, "📈 Big Trend")

    items         = cluster["items"]
    published_dts = cluster["published_dts"]
    oldest   = min(published_dts)
    mins_ago = int((datetime.now(timezone.utc) - oldest).total_seconds() / 60)
    span     = int((max(published_dts) - oldest).total_seconds() / 60)
    coverage = f"{span} mins" if len(published_dts) > 1 else "—"

    # Score articles by relevance to the assessed headline/tag, deduplicate by publisher
    headline_words = set(re.findall(r'\b\w{4,}\b', assessment.get("headline", "").lower()))
    tag_words      = set(re.findall(r'\b\w{4,}\b', assessment.get("tag", "").lower()))
    story_keywords = headline_words | tag_words

    def relevance_score(item):
        title_words = set(re.findall(r'\b\w{4,}\b', item["title"].lower()))
        return len(title_words & story_keywords) + (2 if item["source_tier"] == 1 else 0)

    seen_pubs   = set()
    scored      = sorted([(relevance_score(it), it) for it in items], key=lambda x: (-x[0], x[1]["published_dt"]))
    display     = []
    for score, it in scored:
        pub = _PUB_GROUPS.get(it["source_name"], it["source_name"])
        if pub not in seen_pubs:
            seen_pubs.add(pub)
            display.append((score, it))

    display.sort(key=lambda x: x[1]["published_dt"])
    relevant = [(s, it) for s, it in display if s > 0]
    fallback = [(s, it) for s, it in display if s == 0]
    final    = (relevant + fallback)[:3]

    article_lines = [f"  › [{it['source_name']}] {it['title'][:80]}" for _, it in final]

    # Best URL — prefer T1, pick article whose title best matches cluster headline
    headline_words_url = set(w.lower() for w in (cluster.get("headline", "") or "").split() if len(w) > 4)

    def url_score(item):
        tw = set(w.lower() for w in item.get("title", "").split() if len(w) > 4)
        return len(headline_words_url & tw) + (2 if item.get("source_tier") == 1 else 0) + (1 if not item.get("_from_cache") else 0)

    all_url_items = [it for _, it in display]
    best_url = max(all_url_items, key=url_score)["url"] if all_url_items else ""

    # Sources line
    seen_src, display_sources = set(), []
    for _, it in (relevant + fallback):
        pub = _PUB_GROUPS.get(it["source_name"], it["source_name"])
        if pub not in seen_src:
            seen_src.add(pub)
            display_sources.append(it["source_name"])
    sources_str = " · ".join(display_sources[:6])

    # Tag line — proven_topic shows validated sheet tag + S/A
    tag = assessment.get("tag", "")
    if tier == "proven_topic":
        tag_data = assessment.get("_validated_sheet_tag")
        if tag_data:
            tag_line = f"*Pri Tag:* {tag_data['tag'].title()} _({int(tag_data['sa']):,} S/A)_"
        elif tag:
            tag_line = f"*Pri Tag:* {tag}"
        else:
            tag_line = ""
    else:
        tag_line = f"*Pri Tag:* {tag}" if tag else ""

    lines = [
        f"{tier_label} — *{assessment['headline']}*",
        f"*Sources:* {sources_str}",
        f"*First Seen:* {mins_ago} mins ago  |  Coverage window: {coverage}",
    ]
    if assessment.get("angle"):
        lines.append(f"*Angle:* {assessment['angle']}")
    if tag_line:
        lines.append(tag_line)
    if article_lines:
        lines.append("*In this cluster:*")
        lines.extend(article_lines)
    if best_url:
        lines.append(best_url)
    lines.append("—" * 44)

    try:
        r = requests.post(SLACK_WEBHOOK, json={"text": "\n".join(lines)}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"Slack post failed: {e}")
        return False

# ── Main run ───────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info("Legolas v2 — starting run")
    log.info("=" * 60)

    learnings = load_learnings()
    svc = _sheets_service()

    # 1. Load priority tags from sheet
    priority_tags = load_tag_performance(svc)

    # 2. Process Slack feedback (emoji reactions + thread replies)
    process_feedback(learnings, priority_tags, svc)
    synthesize_editorial_notes(learnings)
    save_learnings(learnings)

    # 3. Load seen GUIDs
    ensure_seen_tab(svc)
    seen_guids = load_seen_guids(svc)

    # 3b. Load recently posted for dupe suppression (Step 3b per PRD)
    recently_posted = load_recently_posted(learnings)

    # 4. Fetch RSS + filter
    all_items = fetch_rss_items(ALL_SOURCES, LOOKBACK_MINS)
    new_items = [i for i in all_items if i["guid"] not in seen_guids]
    log.info(f"{len(new_items)} new items after seen filter")
    new_items = filter_items(new_items)

    # Load cached articles from previous runs for cross-run clustering
    cached_items    = load_article_cache(learnings)
    cached_restored = restore_cached_items(cached_items)
    update_article_cache(learnings, new_items)

    # 5. Python keyword clustering — new + cached context
    all_for_clustering = new_items + cached_restored
    clusters = cluster_items(all_for_clustering)
    clusters = [c for c in clusters if any(not i.get("_from_cache") for i in c["items"])]
    log.info(f"{len(clusters)} clusters with new content")
    clusters = filter_old_single_source_clusters(clusters)

    # 6a. Claude Call 1: find merges + suppress dupes of recently posted stories
    log.info(f"Claude call 1: {len(clusters)} clusters, {len(recently_posted)} recently posted...")
    for i, c in enumerate(clusters, 1):
        c["_local_id"] = i
    merges, dupes, splits = claude_find_merges(clusters, recently_posted)
    clusters = apply_merges(clusters, merges)
    suppress = dupes | splits
    clusters_to_assess = [c for c in clusters if c.get("_local_id", c["id"]) not in suppress]
    if len(clusters) != len(clusters_to_assess):
        log.info(f"Suppressed {len(clusters) - len(clusters_to_assess)} clusters (dupes/splits) → {len(clusters_to_assess)} to assess")

    # 6b. Claude Call 2: editorial assessment
    log.info(f"Claude call 2: assessing {len(clusters_to_assess)} clusters...")
    assessments = claude_assess_clusters(clusters_to_assess, priority_tags, learnings)

    # 7. Python tier enforcement — build will_post list
    will_post:       List[Tuple] = []
    new_seen_items:  List[dict]  = []
    posted_this_run: List[str]   = []

    def _is_mid_run_dupe(headline: str) -> bool:
        words = set(w for w in headline.lower().split() if len(w) > 4)
        for prev in posted_this_run:
            if len(words & set(w for w in prev.lower().split() if len(w) > 4)) >= 3:
                log.info(f"Mid-run dupe suppressed: '{headline[:60]}'")
                return True
        return False

    for cluster, assessment in zip(clusters_to_assess, assessments):
        tier, _ = enforce_tier(assessment, cluster, priority_tags, learnings)
        if tier == "skip":
            log.info(f"⏭ Skip (mw={assessment['mw_relevance']}, claude_tier={assessment['tier']}): {assessment['headline'][:60]}")
            continue
        if _is_mid_run_dupe(assessment.get("headline", "")):
            continue
        will_post.append((cluster, assessment, tier))
        posted_this_run.append(assessment.get("headline", ""))
        for item in cluster["items"]:
            if not item.get("_from_cache"):
                new_seen_items.append(item)

    # 7b. Aragorn: editorial audit + title rewrite
    if will_post:
        log.info(f"Aragorn: auditing {len(will_post)} approved stories...")
        will_post = aragorn_audit(will_post)

    # 8. Post approved stories
    posted = 0
    for cluster, assessment, tier in will_post:
        cluster_urls   = [it["url"]   for it in cluster["items"] if it.get("url")]
        cluster_titles = [it["title"] for it in cluster["items"] if it.get("title")]

        if POST_TO_SLACK:
            if post_to_slack(cluster, assessment, tier):
                posted += 1
                record_posted_story(learnings, assessment["headline"], assessment.get("tag", ""), cluster_urls, cluster_titles)
                posted_this_run.append(assessment["headline"])
                log.info(f"✅ Posted [{tier}] mw={assessment['mw_relevance']}: {assessment['headline'][:60]}")
        else:
            log.info(f"[DRY RUN] {tier} mw={assessment['mw_relevance']}: {assessment['headline'][:70]}")
            posted += 1
            record_posted_story(learnings, assessment["headline"], assessment.get("tag", ""), cluster_urls, cluster_titles)
            posted_this_run.append(assessment["headline"])

    # 9. Write seen GUIDs + save learnings
    if new_seen_items:
        append_seen_guids(svc, list({i["guid"]: i for i in new_seen_items}.values()))
    save_learnings(learnings)

    log.info(f"Run complete — {posted} stories posted")
    return {
        "clusters_total":    len(clusters),
        "clusters_assessed": len(assessments),
        "clusters_posted":   posted,
        "new_items":         len(new_items),
    }


if __name__ == "__main__":
    output = run()
    print(f"\nClusters found:    {output['clusters_total']}")
    print(f"Clusters assessed: {output['clusters_assessed']}")
    print(f"Clusters posted:   {output['clusters_posted']}")
    print(f"New items seen:    {output['new_items']}")
