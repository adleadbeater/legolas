"""Hard content filters (no AI). Lifted from legolas.py so the Elves share them."""
from __future__ import annotations

import re

SKIP_RE = re.compile(
    r"\b(switch\s*2|xbox|playstation\s*5|ps5|nintendo|steam sale|pre.?order"
    r"|discount|deals?|buy now|save \$|power station|projector|3d printer"
    r"|nba|nfl|mlb|nhl|fifa|premier league|champions league|f1 racing"
    r"|stock market|earnings report|quarterly results|stock price|shares (?:rise|fall|jump)"
    r"|obituary|dies at \d+|passed away"
    r"|auction(?:ed|s)?|prop sale)\b",
    re.IGNORECASE,
)
ENTERTAINMENT_RE = re.compile(
    r"\b(actor|actress|director|producer|writer|showrunner|singer|musician"
    r"|filmmaker|comedian|star|cast|oscar|emmy|grammy)\b",
    re.IGNORECASE,
)
LISTICLE_TITLE_RE = re.compile(
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
LISTICLE_URL_RE = re.compile(
    r"/(lists?|rankings?|best-of|top-\d|roundup|guide|preview-\d|anticipated)/",
    re.IGNORECASE,
)


def keep(item: dict) -> bool:
    t = item.get("title", "")
    if SKIP_RE.search(t) and not ENTERTAINMENT_RE.search(t):
        return False
    if LISTICLE_TITLE_RE.search(t):
        return False
    if LISTICLE_URL_RE.search(item.get("url", "")):
        return False
    return True


def filter_items(items: list) -> list:
    return [i for i in items if keep(i)]
