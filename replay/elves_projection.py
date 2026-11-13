"""Phase-2 cost projection for the Little Elves and Celebrimbor, grounded in the same
replay corpus (no AI): how many shared-store articles each draft topic matches per day,
and which outlets carry them first. Model costs use the per-card / per-item costs the
Legolas replay measured, priced from config/pricing.yaml.

  python replay/elves_projection.py --report replay/out/replay-report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout.ledger import Pricing, Usage  # noqa: E402
from scout.outlets import Outlets  # noqa: E402
from scout.textutil import title_tokens, jaccard  # noqa: E402

ET = ZoneInfo("America/New_York")
TOPICS = {  # draft match terms from the Elves pitch (section 1)
    "horror": {"kind": "genre", "terms": ["horror", "slasher", "supernatural thriller", "blumhouse", "a24",
                                          "mike flanagan", "sam raimi", "scream", "halloween", "evil dead",
                                          "conjuring", "28 years later", "final destination", "saw "]},
    "scifi": {"kind": "genre", "terms": ["sci-fi", "science fiction", "space opera", "alien", "dune", "star trek",
                                         "the expanse", "blade runner", "star wars", "predator", "foundation",
                                         "3 body problem", "andor", "fallout"]},
    "sanderson": {"kind": "creator", "terms": ["brandon sanderson", "mistborn", "stormlight", "cosmere", "dragonsteel"]},
    "lego": {"kind": "brand", "terms": ["lego"]},
}


def matches(title, terms):
    t = " " + title.lower() + " "
    return any(x in t for x in terms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="replay/out/corpus.json")
    ap.add_argument("--report", default="replay/out/replay-report.json")
    ap.add_argument("--days", default="2026-11-09,2026-11-10,2026-11-11,2026-11-12")
    a = ap.parse_args()
    days = a.days.split(",")
    o = Outlets()
    arts = [x for x in json.load(open(a.corpus))["articles"]
            if datetime.fromisoformat(x["published_dt"]).astimezone(ET).date().isoformat() in days]
    nd = len(days)
    out = {}
    first_by_topic = defaultdict(Counter)
    all_by_topic = defaultdict(Counter)
    for name, t in TOPICS.items():
        hits = sorted([x for x in arts if matches(x["title"], t["terms"])], key=lambda x: x["published_dt"])
        # cluster by headline similarity to count stories and who had each first
        stories = []
        for h in hits:
            tok = title_tokens(h["title"])
            for s in stories:
                if jaccard(tok, s["tok"]) >= 0.3:
                    s["items"].append(h)
                    break
            else:
                stories.append({"tok": tok, "items": [h]})
        for s in stories:
            first_by_topic[name][o.canonical(s["items"][0]["source_name"])] += 1
        for h in hits:
            all_by_topic[name][o.canonical(h["source_name"])] += 1
        out[name] = {"matched_items_per_day": round(len(hits) / nd, 1),
                     "stories_per_day": round(len(stories) / nd, 1),
                     "top_outlets": all_by_topic[name].most_common(8),
                     "first_reporter": first_by_topic[name].most_common(8)}
    # /Film specifically
    sf = [x for x in arts if o.canonical(x["source_name"]) == "Slash Film"]
    sf_sci = [x for x in sf if matches(x["title"], TOPICS["scifi"]["terms"])]
    out["slash_film"] = {"items_per_day": round(len(sf) / nd, 1), "scifi_items_per_day": round(len(sf_sci) / nd, 1),
                         "scifi_first_reports": first_by_topic["scifi"].get("Slash Film", 0),
                         "scifi_stories_total": sum(first_by_topic["scifi"].values()),
                         "examples": [x["title"] for x in sf_sci[:8]]}

    # cost projection
    p = Pricing()
    rep = json.load(open(a.report)) if Path(a.report).exists() else None
    if rep:
        c = rep["v2"]["15min"]["per_day_by_call"]["card:claude-sonnet-5-5"]
        cards = rep["v2"]["15min"]["per_day"]["carded"]
        card_usd = c["usd"] / max(cards, 1)
    else:
        card_usd = 0.0018
    haiku_item = p.cost("claude-haiku-5-5", Usage(input_tokens=180, output_tokens=60))
    search_usd = p.cost("claude-haiku-5-5", Usage(input_tokens=6000, output_tokens=60, web_searches=1))
    n_topics = len(TOPICS)
    doubtful = sum(out[t]["matched_items_per_day"] for t in ("horror", "scifi"))  # genre hits get a Haiku check
    elves = {
        "haiku_genre_checks": round(doubtful * haiku_item, 4),
        "sonnet_cards (3/topic/day, 2x Legolas card size, Batches -50%)": round(3 * n_topics * card_usd * 2 * 0.5, 4),
        "web_search (cap 3/topic/day, worst case all used)": round(3 * n_topics * search_usd, 4),
        "extraction of shared items": 0.0,
        "own-feed items (~100/day new, Haiku)": round(100 * haiku_item, 4),
    }
    elves["total"] = round(sum(elves.values()), 4)
    celeb = {
        "polling (watchlist, release list diff)": 0.0,
        "haiku change detection (~30 changed items/day)": round(30 * haiku_item * 2, 4),
        "sonnet writes (~5/day incl. TV nights)": round(5 * card_usd * 2, 4),
        "web search after confirmed change (cap 5/day)": round(5 * search_usd, 4),
        "discourse pass 3x/day via Batches (Haiku detect + Sonnet write)": round(3 * (20 * haiku_item + card_usd * 2) * 0.5, 4),
    }
    celeb["total"] = round(sum(celeb.values()), 4)
    out["projection_usd_per_day"] = {"elves_hourly": elves, "celebrimbor": celeb,
                                     "note": "Hourly vs daily Elves cost the same: matching is Python and model calls only see new matches."}
    print(json.dumps(out, indent=2))
    Path("replay/out/elves-projection.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
