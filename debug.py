"""
Legolas v2 — Debug Runner
Run: python debug.py

Fetches and clusters articles, runs both Claude calls, shows what would post.
Does NOT write to Seen tab or learnings by default.
Set POST_TO_SLACK_DBG = True to actually post.
"""

import re
from datetime import datetime, timezone

from legolas import (
    ALL_SOURCES, TIER_1_SOURCES, TIER_2_SOURCES,
    MW_RELEVANCE_MIN, LEGOLAS_SPECIAL_MIN,
    _sheets_service, _PUB_GROUPS,
    load_tag_performance, load_learnings, save_learnings,
    load_seen_guids, ensure_seen_tab,
    fetch_rss_items, filter_items,
    load_article_cache, restore_cached_items,
    cluster_items, match_cluster_tags,
    load_recently_posted,
    claude_find_merges, apply_merges,
    claude_assess_clusters,
    enforce_tier, aragorn_audit, post_to_slack, record_posted_story,
)

# ── Debug settings ─────────────────────────────────────────────────────────────
IGNORE_SEEN       = True    # True = reprocess all items regardless of Seen tab
LOOKBACK_MINS_DBG = 120     # lookback window for debug (2 hours default)
POST_TO_SLACK_DBG = False   # Set True to actually post to Slack


def probe_feeds():
    import requests, feedparser
    print(f"\n{'═' * 60}")
    print("FEED PROBE")
    print("═" * 60)
    ok, broken = [], []
    for name, url in ALL_SOURCES.items():
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Legolas/2.0"})
            r.raise_for_status()
            feed  = feedparser.parse(r.content)
            count = len(feed.entries)
            first = feed.entries[0].title[:50] if feed.entries else "(empty)"
            tier  = "T1" if name in TIER_1_SOURCES else "T2"
            ok.append(name)
            print(f"  ✅ [{tier}] {name:<30} {count:>3} entries  │ '{first}'")
        except Exception as e:
            broken.append((name, url, str(e)[:60]))
            tier = "T1" if name in TIER_1_SOURCES else "T2"
            print(f"  ❌ [{tier}] {name:<30} {str(e)[:60]}")

    print(f"\n  {len(ok)} working, {len(broken)} broken")
    if broken:
        print("\n  ── Broken sources ──")
        for name, url, err in broken:
            print(f"  {name:<30} {url}")


def run_debug():
    print("█" * 60)
    print("  LEGOLAS v2 DEBUG RUN")
    print("█" * 60)

    svc = _sheets_service()
    priority_tags = load_tag_performance(svc)
    learnings     = load_learnings()

    # ── Tag sheet summary ──────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print("TAG SHEET SUMMARY")
    print("═" * 60)
    print(f"  {len(priority_tags)} priority tags loaded (S/A ≥ 5,000, FR < 25%)")
    if priority_tags:
        top = sorted(priority_tags.items(), key=lambda x: x[1]["sa"], reverse=True)[:10]
        print(f"\n  Top 10:")
        print(f"  {'Tag':<40} {'S/A':>8}  {'FR':>5}")
        print(f"  {'-' * 55}")
        for tag, s in top:
            print(f"  {tag:<40} {int(s['sa']):>8,}  {s['fr']:>4.1f}%")

    # ── Seen tab ───────────────────────────────────────────────────
    ensure_seen_tab(svc)
    seen_guids = load_seen_guids(svc)
    print(f"\n{'═' * 60}")
    print("SEEN TAB")
    print("═" * 60)
    print(f"  {len(seen_guids)} GUIDs in Seen tab")
    if IGNORE_SEEN:
        print("  ⚠️  IGNORE_SEEN=True — Seen tab is read-only this run")

    # ── Feed probe ─────────────────────────────────────────────────
    probe_feeds()

    # ── Fetch ──────────────────────────────────────────────────────
    print(f"\n{'═' * 60}")
    print(f"FETCHING RSS (last {LOOKBACK_MINS_DBG} mins)")
    print("═" * 60)

    all_items = fetch_rss_items(ALL_SOURCES, LOOKBACK_MINS_DBG)
    if IGNORE_SEEN:
        new_items = all_items
        print(f"\n  ⚠️  IGNORE_SEEN=True — reprocessing all {len(new_items)} items")
    else:
        new_items = [i for i in all_items if i["guid"] not in seen_guids]
        print(f"\n  {len(new_items)} new items (after seen filter)")

    new_items = filter_items(new_items)
    print(f"  {len(new_items)} items after hard filter")

    cached_items    = load_article_cache(learnings)
    cached_restored = restore_cached_items(cached_items)
    if cached_restored:
        print(f"  {len(cached_restored)} cached articles from previous runs added for clustering")

    if not new_items:
        print("\n⚠️  No new items to process.")
        return

    # ── Cluster ────────────────────────────────────────────────────
    all_for_clustering = new_items + cached_restored
    clusters = cluster_items(all_for_clustering)
    clusters = [c for c in clusters if any(not i.get("_from_cache") for i in c["items"])]

    print(f"\n{'═' * 60}")
    print(f"PYTHON CLUSTERS ({len(clusters)} clusters from {len(new_items)} items)")
    print("═" * 60)
    for i, c in enumerate(clusters):
        n_t1 = sum(1 for it in c["items"] if it["source_tier"] == 1)
        n_t2 = sum(1 for it in c["items"] if it["source_tier"] == 2)
        matched = match_cluster_tags(c, priority_tags, learnings)
        tag_str = ", ".join(f"{m['tag']} ({int(m['sa']):,})" for m in matched[:3]) if matched else "—"

        print(f"\n  [{i + 1}] {c['headline'][:75]}")
        print(f"       Sources: {', '.join(c['sources'])}")
        print(f"       T1: {n_t1}  T2: {n_t2}  Total: {n_t1 + n_t2}")
        print(f"       Sheet tags: {tag_str}")
        for it in sorted(c["items"], key=lambda x: x["published_dt"]):
            age   = int((datetime.now(timezone.utc) - it["published_dt"]).total_seconds() / 60)
            tier  = "T1" if it["source_tier"] == 1 else "T2"
            cache = " [cached]" if it.get("_from_cache") else ""
            print(f"         [{tier}] {it['source_name']:<22} {age:>3}m ago | {it['title'][:60]}{cache}")

    print(f"\n{'═' * 60}")
    print(f"SUMMARY: {len(clusters)} clusters | {len(new_items)} items")
    print("═" * 60)

    # ── Claude Call 1 ──────────────────────────────────────────────
    recently_posted = load_recently_posted(learnings)
    print(f"\n  Claude call 1: {len(clusters)} clusters, {len(recently_posted)} recently posted stories...")
    for i, c in enumerate(clusters, 1):
        c["_local_id"] = i
    merges, dupes, splits = claude_find_merges(clusters, recently_posted)
    clusters = apply_merges(clusters, merges)

    if dupes:
        print(f"  ⛔ Suppressed {len(dupes)} dupe clusters (same event already posted): {dupes}")
    if splits:
        print(f"  ✂️  Suppressed {len(splits)} mixed clusters (different stories lumped together): {splits}")

    suppress = dupes | splits
    clusters_to_assess = [c for c in clusters if c.get("_local_id", c["id"]) not in suppress]
    if len(clusters) != len(clusters_to_assess):
        print(f"  {len(clusters_to_assess)} clusters remain after dupe/split suppression")
    elif merges:
        print(f"  Merged into {len(clusters_to_assess)} clusters")
    else:
        print(f"  No merges found — {len(clusters_to_assess)} clusters unchanged")

    # ── Claude Call 2 ──────────────────────────────────────────────
    print(f"\n  Claude call 2: assessing {len(clusters_to_assess)} clusters...")
    assessments = claude_assess_clusters(clusters_to_assess, priority_tags, learnings)

    print(f"\n{'═' * 60}")
    print(f"CLAUDE ASSESSMENT ({len(assessments)} clusters)")
    print("═" * 60)

    will_post: list[tuple] = []
    posted_this_run_dbg: list[str] = []

    def _is_mid_run_dupe_dbg(headline: str) -> bool:
        words = set(w for w in headline.lower().split() if len(w) > 4)
        for prev in posted_this_run_dbg:
            if len(words & set(w for w in prev.lower().split() if len(w) > 4)) >= 3:
                print(f"  🚫 Mid-run dupe suppressed: '{headline[:60]}'")
                return True
        return False

    for cluster, assessment in zip(clusters_to_assess, assessments):
        tier, matched_by_name = enforce_tier(assessment, cluster, priority_tags, learnings)
        mw = assessment["mw_relevance"]

        original_tier = assessment["tier"]
        demoted = tier != original_tier

        if tier == "skip" or mw < MW_RELEVANCE_MIN:
            suffix = f" ⬇️ demoted from {original_tier}" if demoted else ""
            print(f"\n  ⏭  SKIP (mw={mw}, claude_tier={original_tier}){suffix}")
        elif _is_mid_run_dupe_dbg(assessment.get("headline", "")):
            print(f"\n  ⏭  SKIP (mid-run dupe)")
        else:
            tier_icons = {
                "trending":        "📈 Big Trend",
                "proven_topic":    "🎯 MW Proven Topic",
                "legolas_special": "⭐ Legolas Special",
            }
            print(f"\n  ✅ POST — {tier_icons.get(tier, tier)}")
            will_post.append((cluster, assessment, tier))
            posted_this_run_dbg.append(assessment.get("headline", ""))

        print(f"       headline : {assessment['headline'][:70]}")
        print(f"       mw_score : {mw}/10  |  claude: {original_tier}  |  final: {tier}")
        if assessment.get("tag"):
            if tier == "proven_topic":
                tag_data = assessment.get("_validated_sheet_tag")
                if tag_data:
                    print(f"       sheet_tag: {tag_data['tag']} ({int(tag_data['sa']):,} S/A)  ← validated")
                else:
                    print(f"       tag      : {assessment['tag']} (no sheet match)")
            else:
                print(f"       tag      : {assessment['tag']}")
        if cluster.get("_merged_from"):
            print(f"       merged   : clusters {cluster['_merged_from']}")
        if assessment.get("angle"):
            print(f"       angle    : {assessment['angle'][:80]}")
        if assessment.get("note"):
            print(f"       note     : {assessment['note'][:80]}")
        print(f"       sources  : {', '.join(cluster['sources'])}")

    print(f"\n  After assessment: {len(will_post)}/{len(assessments)} stories would post")

    # ── Aragorn ────────────────────────────────────────────────────
    if will_post:
        print(f"\n{'═' * 60}")
        print(f"ARAGORN AUDIT ({len(will_post)} stories)")
        print("═" * 60)
        will_post = aragorn_audit(will_post)
        print(f"\n  After Aragorn: {len(will_post)} stories survive")

    if recently_posted:
        print(f"\n  Recently posted (last 6h — used for dupe detection):")
        for s in recently_posted[-10:]:
            age = int((datetime.now(timezone.utc) - datetime.fromisoformat(s["posted_at"])).total_seconds() / 60)
            print(f"    {age:>4}m ago | {s['headline'][:70]}")

    # ── Post (if enabled) ──────────────────────────────────────────
    if POST_TO_SLACK_DBG and will_post:
        print("\n  Posting to Slack...")
        posted = 0
        for cluster, assessment, tier in will_post:
            if post_to_slack(cluster, assessment, tier):
                posted += 1
                cluster_urls   = [it["url"]   for it in cluster["items"] if it.get("url")]
                cluster_titles = [it["title"] for it in cluster["items"] if it.get("title")]
                record_posted_story(learnings, assessment["headline"], assessment.get("tag", ""), cluster_urls, cluster_titles)
        save_learnings(learnings)
        print(f"  Posted {posted} messages.")
    else:
        print(f"\n  ℹ️  POST_TO_SLACK_DBG=False — nothing sent to Slack.")
        print("     Set POST_TO_SLACK_DBG = True at the top of debug.py when ready.")


if __name__ == "__main__":
    run_debug()
