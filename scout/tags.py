"""MW tag matching: whole tags against extracted ENTITIES, never substrings of headlines.

Old Legolas matched tags as substrings of headline text, so the MW tag for the show
"From" matched the word "from" in any headline (40 false "Proven Topic" cards) and
"Legends" validated an Avatar card. Here a tag only matches when it IS one of the
story's named entities (or a multi-word tag sits whole inside one).
"""
from __future__ import annotations

from typing import Dict, Iterable, List

from scout.textutil import entity_key, contains_phrase

DEFAULT_STOPWORD_TAGS = {
    "it", "you", "your", "men", "women", "man", "her", "him", "his", "she", "they",
    "hope", "troy", "us", "we", "now", "new", "one", "two", "all", "out", "up", "from",
    "legends", "the", "down", "off", "on", "in", "and", "for", "but", "not", "yes", "go",
    "dark", "house", "doc", "her", "them", "us", "home", "friends", "lost", "you",
}


def is_junk_tag(tag: str, stopwords: Iterable[str] = DEFAULT_STOPWORD_TAGS) -> bool:
    t = (tag or "").strip().lower()
    if len(t) <= 2 or not any(ch.isalnum() for ch in t):
        return True
    return " " not in t and t in set(stopwords)


def match_tags(entities: Iterable[str], priority_tags: Dict[str, dict],
               generic: Iterable[str] = ()) -> List[dict]:
    generic = {g.lower() for g in generic}
    ent_keys = {entity_key(e): e for e in entities if e}
    out = []
    for tag, stats in priority_tags.items():
        tk = entity_key(tag)
        if not tk or tag.lower() in generic:
            continue
        hit = tk in ent_keys
        if not hit and " " in tk:
            # multi-word tag wholly inside a longer entity name ("Star Wars" in
            # "Star Wars: Maul") - still a whole phrase, never a common word
            hit = any(contains_phrase(name, tag) for name in ent_keys.values())
        if hit:
            out.append({"tag": tag, "sa": stats.get("sa", 0), "fr": stats.get("fr", 0)})
    out.sort(key=lambda m: -m["sa"])
    return out[:5]
