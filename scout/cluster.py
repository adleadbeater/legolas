"""Entity + event clustering into the story store (no AI).

An article joins an open story when they share a STRONG entity (a named title or
person; a franchise only when the article names no title or person) AND the same
event family. Shared franchise words alone ("Harry Potter", "Crystal Lake") never join
two articles, which is what produced the Chocolate-Frog / LEGO-Hogwarts cards. Same
entity + different event becomes a separate story, linked as `related`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from scout.store import Store
from scout.textutil import entity_key

FAMILY = {
    "casting": "cast", "exit": "cast",
    "trailer": "promo", "first_look": "promo",
    "box_office": "perf", "streaming_ratings": "perf",
    "development": "dev", "production": "dev", "deal": "dev",
    "release_date": "date", "renewal": "renewal", "cancellation": "cancel",
    "reviews_reception": "reception", "award": "award", "death": "death",
    "legal_controversy": "legal", "interview": "talk", "merch_tie_in": "merch",
    "feature": "feature", "other": "other",
}
LOOSE = {"feature", "other", "talk"}  # need 2 shared strong entities to join


def strong_entities(ext: dict) -> List[str]:
    s = [entity_key(x) for x in (ext.get("ti") or []) + (ext.get("pe") or [])]
    if not s:
        s = [entity_key(x) for x in ext.get("fr") or []]
    return [x for x in s if x]


def _story_strong(st: dict) -> set:
    return {e["key"] for e in st["entities"] if e["type"] in ("ti", "pe")} or \
           {e["key"] for e in st["entities"] if e["type"] == "fr"}


def _merge_entities(st: dict, ext: dict) -> None:
    by_key = {(e["type"], e["key"]): e for e in st["entities"]}
    for typ in ("ti", "pe", "fr", "pl"):
        for name in ext.get(typ) or []:
            k = entity_key(name)
            if not k:
                continue
            e = by_key.get((typ, k))
            if e:
                e["n"] += 1
            else:
                e = {"type": typ, "key": k, "name": name, "n": 1}
                st["entities"].append(e)
                by_key[(typ, k)] = e
    st["entities"].sort(key=lambda e: (-e["n"], e["type"]))


def _primary(st: dict) -> str:
    for typ in ("ti", "pe", "fr", "pl"):
        c = [e for e in st["entities"] if e["type"] == typ]
        if c:
            return max(c, key=lambda e: e["n"])["name"]
    return ""


def attach(store: Store, st: dict, item: dict, ext: dict) -> None:
    pub = item["published_dt"]
    _merge_entities(st, ext)
    st["primary_entity"] = _primary(st)
    evs = Counter(st.get("_events", {}))
    evs[ext["ev"]] += 1
    st["_events"] = dict(evs)
    st["event_type"] = evs.most_common(1)[0][0]
    st["facts"].append({"fact": ext.get("fact") or item["title"], "source": item["source_name"],
                        "tier": item.get("source_tier", 3), "url": item.get("url", ""),
                        "at": pub.isoformat(), "guid": item["guid"], "ev": ext["ev"],
                        "rum": ext.get("rum", 0), "new": True})
    if not st["key_fact"] or item.get("source_tier", 3) < st.get("_key_fact_tier", 9):
        st["key_fact"] = ext.get("fact") or item["title"]
        st["_key_fact_tier"] = item.get("source_tier", 3)
        st["key_fact_source"] = item["source_name"]
    st["relevance"] = max(st.get("relevance", 0), ext.get("rel", 0))
    st["is_news"] = bool(st.get("is_news")) or bool(ext.get("news"))
    st["rumour"] = all(f.get("rum") for f in st["facts"])
    if pub.isoformat() > st["last_item_at"]:
        st["last_item_at"] = pub.isoformat()
    if pub.isoformat() < st["first_seen"]:
        st["first_seen"] = pub.isoformat()
    store.set_item_story(item["guid"], st["id"])


def assign(store: Store, items: List[Tuple[dict, dict]], now: datetime, open_hours: int = 72,
           lane: str = "legolas") -> Tuple[Dict[str, dict], set]:
    """items: [(item, extraction)]. Returns (touched stories by id, ids created this run)."""
    stories = {s["id"]: s for s in store.open_stories(now, open_hours)}
    index = defaultdict(set)
    for s in stories.values():
        for k in _story_strong(s):
            index[k].add(s["id"])
    touched: Dict[str, dict] = {}
    created = set()
    for item, ext in sorted(items, key=lambda p: p[0]["published_dt"]):
        keys = set(strong_entities(ext))
        fam = FAMILY.get(ext["ev"], "other")
        best, best_score = None, 0
        related = set()
        for sid in {sid for k in keys for sid in index.get(k, ())}:
            s = stories[sid]
            shared = len(keys & _story_strong(s))
            if FAMILY.get(s["event_type"], "other") != fam:
                related.add(sid)
                continue
            need = 2 if fam in LOOSE else 1
            if shared >= need and shared > best_score:
                best, best_score = s, shared
        if best is None:
            best = store.new_story(item["published_dt"], lane)
            best["first_seen"] = best["last_item_at"] = item["published_dt"].isoformat()
            stories[best["id"]] = best
            created.add(best["id"])
        attach(store, best, item, ext)
        best["related"] = sorted(set(best.get("related", [])) | (related - {best["id"]}))[:10]
        best["updated_at"] = now.isoformat()
        for k in _story_strong(best):
            index[k].add(best["id"])
        touched[best["id"]] = best
    for s in touched.values():
        store.save_story(s)
    store.commit()
    return touched, created
