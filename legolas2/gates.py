"""Mechanical posting gates and ranking (no AI)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from scout.outlets import Outlets
from scout.tags import match_tags
from scout.textutil import entity_key

TIER_LABEL = {"trending": "Big Trend", "proven_topic": "Proven Topic", "legolas_special": "Special Pick"}


def evaluate(story: dict, items: List[dict], outlets: Outlets, gates: dict,
             priority_tags: Dict[str, dict], generic_tags: List[str],
             watched: Optional[Dict[str, dict]], now: datetime,
             baity: Optional[set] = None) -> Tuple[str, dict]:
    info: dict = {}
    weighted = outlets.weighted_sources(items)
    info["weighted_sources"] = weighted
    names = [e["name"] for e in story["entities"]]
    rel = story.get("relevance", 0)
    # watched topics (perennials / new releases): exact entity match, never substring
    boost, topic = 0, None
    if watched:
        keys = {entity_key(n) for n in names}
        for k, meta in watched.items():
            if entity_key(k) in keys and meta.get("boost", 0) > boost:
                boost, topic = meta["boost"], meta
    if topic:
        info["topic"] = topic
        rel = min(10, rel + boost)
    info["relevance"] = rel
    tags = [t for t in match_tags(names, priority_tags, generic_tags)
            if t["sa"] >= gates.get("tag_sa_min", 5000) and t["fr"] < gates.get("tag_fr_max", 25)]
    info["tags"] = tags
    if not story.get("is_news") or rel < gates.get("min_relevance", 6):
        return "skip", info
    credible = [i for i in items if outlets.tier_of(i["source_name"]) <= 2 and not outlets.is_self(i["source_name"])]
    if not credible:
        return "skip", info
    if weighted < 1.0:
        first = min(i["published_dt"] for i in items)
        if now - first > timedelta(hours=gates.get("max_single_source_age_hours", 24)):
            return "skip", info
    if weighted >= gates.get("trending_min_weighted_sources", 4) and not topic:
        return "trending", info
    if tags:
        return "proven_topic", info
    if rel >= gates.get("special_min_relevance", 7):
        srcs = {outlets.canonical(i["source_name"]) for i in credible}
        if baity and srcs and srcs <= baity:
            return "skip", info
        return "legolas_special", info
    return "skip", info


def rank_score(tier: str, info: dict, mom: dict, mw_status: str, kind: str) -> float:
    s = info.get("relevance", 0)
    s += {"trending": 2.0, "proven_topic": 1.5, "legolas_special": 1.0}.get(tier, 0)
    s += min(mom.get("sources_in_window", 0), 4) * 0.5
    s += {"not_on_mw": 1.5, "follow_up": 0.5, "covered": -2.0}.get(mw_status, 0)
    if kind == "update":
        s -= 0.5
    return round(s, 2)
