"""MW coverage check (no AI): Not on MW / Follow-up / Covered, plus "sister has it".

MW's articles come from its public Google News sitemap (titles + publish times),
accumulated in the store for `window_days`. A story is
  Covered     MW published on the same entity AND the same event family
  Follow-up   MW covered the entity recently but a different event, OR MW covered
              this event and a newer fact has arrived since MW's article
  Not on MW   neither
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from scout.cluster import FAMILY
from scout.extract import heuristic_extract
from scout.fetch import slug_words
from scout.tags import DEFAULT_STOPWORD_TAGS
from scout.textutil import contains_phrase, entity_key

# Single-word titles that are also ordinary words ("From", "Legends", "Ink") can only
# match MW's title as a quoted title, never as a word in a sentence.
COMMON_WORDS = set(DEFAULT_STOPWORD_TAGS) | {"ink", "it", "us", "them", "up", "smile", "barbarian",
                                            "elf", "frozen", "tag", "nope", "fallout", "alien", "him"}

NOT_ON_MW, FOLLOW_UP, COVERED = "not_on_mw", "follow_up", "covered"
LABEL = {NOT_ON_MW: "Not on MW", FOLLOW_UP: "Follow-up", COVERED: "Covered"}


def _names(story: dict) -> List[str]:
    names = [e["name"] for e in story["entities"] if e["type"] in ("ti", "pe")]
    return names or [e["name"] for e in story["entities"] if e["type"] == "fr"]


def check(story: dict, mw_articles: List[dict], sister_items: Optional[List[dict]] = None) -> dict:
    names = [n for n in _names(story) if len(n) > 3]
    fam = FAMILY.get(story["event_type"], "other")
    same_event, same_entity = [], []
    for a in mw_articles:
        hay = a["title"] + " " + slug_words(a["url"])
        hits = []
        for n in names:
            if " " not in n.strip() and entity_key(n) in COMMON_WORDS:
                if any(q + n + q2 in a["title"] for q, q2 in (("'", "'"), ("\u2018", "\u2019"), ('"', '"'))):
                    hits.append(n)
            elif contains_phrase(hay, n):
                hits.append(n)
        if not hits:
            continue
        a_fam = FAMILY.get(heuristic_extract({"title": a["title"]})["ev"], "other")
        (same_event if a_fam == fam else same_entity).append(a)
    status, match = NOT_ON_MW, None
    if same_event:
        match = max(same_event, key=lambda a: a["published_dt"])
        newest_fact = max(f["at"] for f in story["facts"]) if story["facts"] else ""
        status = FOLLOW_UP if newest_fact > match["published_dt"].isoformat() and \
            story.get("_new_dev_since_mw") else COVERED
    elif same_entity:
        match = max(same_entity, key=lambda a: a["published_dt"])
        status = FOLLOW_UP
    sister = sorted({i["source_name"] for i in (sister_items or [])})
    return {"status": status, "label": LABEL[status],
            "mw_title": match["title"] if match else None,
            "mw_url": match["url"] if match else None,
            "mw_at": match["published_dt"].isoformat() if match else None,
            "sister_has_it": sister}
