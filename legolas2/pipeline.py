"""Legolas V2 story desk: one run.

  collect (no AI) -> seen-set (persisted; only NEW articles go on) -> filter (no AI)
  -> Haiku extraction of new articles only -> entity+event clustering into the story
  store (no AI) -> gates, momentum, MW check (no AI) -> change detection on posted
  stories (Python, Haiku only for ambiguous cases) -> rank -> optional gated web
  search -> Sonnet cards for the top N -> lean Slack card + JSON feed row.

Cost therefore scales with new articles and new developments, not with how often the
job runs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from scout.cluster import assign
from scout.extract import extract_items
from scout.feed import FeedWriter
from scout.filters import keep
from scout.ledger import Ledger
from scout.llm import LLM
from scout.momentum import momentum
from scout.mwcheck import check as mw_check
from scout.outlets import Outlets
from scout.store import Store
from scout.websearch import verify

from legolas2 import card as cardmod
from legolas2 import changes, gates, slack

log = logging.getLogger("legolas.pipeline")


@dataclass
class Ctx:
    cfg: dict
    store: Store
    ledger: Ledger
    llm: LLM
    outlets: Outlets
    feed: FeedWriter
    feed_names: set
    priority_tags: Dict[str, dict] = field(default_factory=dict)
    generic_tags: List[str] = field(default_factory=list)
    watched: Dict[str, dict] = field(default_factory=dict)
    baity: set = field(default_factory=set)
    lane: str = "legolas"


def _sources_line(items: List[dict], outlets: Outlets) -> str:
    first = min(items, key=lambda i: (i["published_dt"], outlets.tier_of(i["source_name"])))
    others = {outlets.independence_key(i["source_name"]) for i in items} - {outlets.independence_key(first["source_name"])}
    t = outlets.tier_of(first["source_name"])
    s = f"{outlets.canonical(first['source_name'])} (T{t}, first)"
    return s + (f" +{len(others)}" if others else "")


def _best_url(items: List[dict], outlets: Outlets) -> str:
    good = [i for i in items if i.get("url")] or items
    return min(good, key=lambda i: (outlets.tier_of(i["source_name"]), i["published_dt"])).get("url", "")


def _feed_row(c: dict, now: datetime, outlets: Outlets, status: str) -> dict:
    st, card = c["story"], c.get("card") or {}
    items = c["items"]
    return {
        "id": st["id"], "rev": st.get("feed_rev", 0) + 1, "agent": "Legolas",
        "title": card.get("title") or c["key_fact"][:95],
        "what_happened": card.get("what_happened") or c["key_fact"],
        "first_seen": st["first_seen"], "updated_at": now.isoformat(), "status": status,
        "mw_status": c["mw"]["status"], "mw_match": c["mw"].get("mw_title"),
        "sources": [{"name": outlets.canonical(i["source_name"]), "url": i.get("url", ""),
                     "tier": outlets.tier_of(i["source_name"])} for i in items[:8]],
        "source_tier": min(outlets.tier_of(i["source_name"]) for i in items),
        "event_type": st["event_type"], "entities": [e["name"] for e in st["entities"][:6]],
        "angles": card.get("angles", []), "tier_label": gates.TIER_LABEL.get(c["tier"], ""),
        "momentum": c.get("momentum", {}).get("label"), "rumour": bool(st.get("rumour")),
        "update_note": c.get("dev_fact") if c["kind"] == "update" else None,
        "topic": card.get("tag") or st.get("primary_entity", ""),
        "url": c.get("url", ""), "related": st.get("related", [])[:5], "auto_approve": True,
    }


def budget_exhausted(ctx: Ctx, where: str, stats: dict) -> bool:
    """Daily $ cap (budget.daily_usd_cap, global for Legolas: every model + web search).

    True when the cap is already spent, or when any call in this run was blocked by it
    (the pre-call worst-case estimate in scout.llm). The run then stops and posts
    NOTHING to Slack or the story feed; the stop is logged and written to the ledger.
    """
    llm = ctx.llm
    if not (llm.budget_hit or llm.remaining() <= 0):
        return False
    spent = ctx.ledger.spent(llm.app)
    if not stats.get("budget_stop"):
        ctx.ledger.record_event(app=llm.app, lane=ctx.lane, event="budget_stop",
                                detail=f"{where}: spent={spent:.4f} cap={llm.cap}")
        log.warning("Legolas daily cap $%.2f reached (spent $%.4f) at %s - stopping, nothing posted",
                    llm.cap, spent, where)
    stats["budget_stop"] = where
    stats["usd_today"] = round(spent, 4)
    return True


def run_once(ctx: Ctx, now: datetime, fetched: List[dict],
             mw_articles: Optional[List[dict]] = None) -> dict:
    cfg, store, outlets = ctx.cfg, ctx.store, ctx.outlets
    stats = {"fetched": len(fetched), "new": 0, "kept": 0, "extracted": 0, "touched": 0,
             "candidates": 0, "carded": 0, "posted": 0, "updates": 0, "covered_rows": 0,
             "killed": 0, "searched": 0}
    why = ctx.llm.ks.reason_off()
    if why:
        ctx.ledger.record_event(app=ctx.llm.app, lane=ctx.lane, event="killswitch", detail=why)
        log.warning("Legolas is switched off (%s) - nothing to do", why)
        stats["killed_switch"] = why
        return stats
    if budget_exhausted(ctx, "run start", stats):
        return stats
    if mw_articles:
        store.add_mw_articles(mw_articles)

    # 1. normalise outlets, drop MW itself; only our own RSS feeds are sources
    #    (Google News was dropped on 13 Nov 2026)
    items, own = [], []
    for it in fetched:
        name = outlets.canonical(it["source_name"])
        if outlets.is_self(name):
            own.append(it)   # MW's own article: evidence for the coverage check, never a source
            continue
        if it.get("via", "rss") != "rss":
            continue  # anything not read from feeds.yaml (e.g. old Google News items)
        items.append({**it, "source_name": name, "source_tier": outlets.tier_of(name)})
    if own:
        store.add_mw_articles(own)

    # 2. seen-set: only articles never seen before go any further
    new = store.unseen(items)
    store.add_items(new, now)
    stats["new"] = len(new)
    kept = [i for i in new if keep(i)]
    stats["kept"] = len(kept)

    # 3. extraction (Haiku) on new articles only, then clustering
    ext = extract_items(ctx.llm, kept, cfg) if kept else {}
    for g, e in ext.items():
        store.set_extraction(g, e, now)
    stats["extracted"] = len(ext)
    touched, created = assign(store, [(i, ext[i["guid"]]) for i in kept], now,
                              cfg.get("clustering", {}).get("open_story_hours", 72), ctx.lane)
    stats["touched"] = len(touched)
    if budget_exhausted(ctx, "after extraction", stats):
        store.commit()   # keep the seen-set so these articles are never re-billed
        return stats

    # pending candidates from earlier runs that missed the top-N cut
    pending = store.kv_get("pending", {})
    for sid, at in list(pending.items()):
        if now - datetime.fromisoformat(at) > timedelta(hours=3):
            pending.pop(sid)
        elif sid not in touched:
            s = store.get_story(sid)
            if s:
                touched[sid] = s

    g = cfg.get("gates", {})
    upd = cfg.get("updates", {})
    day = ctx.ledger.today()
    mw_recent = store.mw_articles(now, cfg.get("mw_check", {}).get("window_days", 7))
    cands, ambiguous = [], []
    for sid, st in touched.items():
        sitems = store.items_for_story(sid)
        if not sitems:
            continue
        tier, info = gates.evaluate(st, sitems, outlets, g, ctx.priority_tags, ctx.generic_tags,
                                    ctx.watched, now, ctx.baity)
        base = {"story": st, "items": sitems, "tier": tier, "info": info,
                "momentum": momentum(sitems, outlets, g.get("momentum_window_minutes", 120)),
                "sources_line": _sources_line(sitems, outlets), "url": _best_url(sitems, outlets),
                "key_fact": st["key_fact"]}
        if st.get("posts", 0) == 0 and not st.get("covered_noted"):
            if tier != "skip":
                cands.append({**base, "kind": "new"})
            continue
        kind, nf = changes.classify(st)
        if kind == "repeat":
            for f in st["facts"]:
                f["new"] = False
            store.save_story(st)
            continue
        last = st.get("last_post_at")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=upd.get("min_gap_minutes", 120)):
            continue  # keep facts flagged new; reconsider after the gap
        if store.count(day, f"updates:{sid}") >= upd.get("max_updates_per_story_per_day", 4):
            continue
        if kind == "dev":
            cands.append({**base, "kind": "update", "dev_fact": nf[0]["fact"], "key_fact": nf[0]["fact"]})
        else:
            ambiguous.append((st, nf, base))
    if ambiguous:
        verdicts = changes.resolve_ambiguous(ctx.llm, [(a[0], a[1]) for a in ambiguous], cfg)
        for st, nf, base in ambiguous:
            v = verdicts.get(st["id"])
            if v:
                fact = v.get("fact") or nf[0]["fact"]
                cands.append({**base, "kind": "update", "dev_fact": fact, "key_fact": fact})
            else:
                for f in st["facts"]:
                    f["new"] = False
                store.save_story(st)

    # 4. MW coverage + ranking (no AI)
    post_covered = cfg.get("mw_check", {}).get("post_covered", False)
    ranked = []
    for c in cands:
        st = c["story"]
        st["_new_dev_since_mw"] = c["kind"] == "update"
        sisters = [i for i in c["items"] if outlets.is_sister(i["source_name"])]
        c["mw"] = mw_check(st, mw_recent, sisters)
        if c["mw"]["status"] == "covered" and c["kind"] == "new" and not post_covered:
            # MW already has it: one feed row so editors can see it, no model call
            ctx.feed.write(_feed_row(c, now, outlets, "covered"))
            st["feed_rev"] = st.get("feed_rev", 0) + 1
            st["covered_noted"] = True
            st["last_post_at"] = now.isoformat()
            for f in st["facts"]:
                f["new"] = False
            st["mw_status"] = "covered"
            store.save_story(st)
            stats["covered_rows"] += 1
            pending.pop(st["id"], None)
            continue
        c["score"] = gates.rank_score(c["tier"], c["info"], c["momentum"], c["mw"]["status"], c["kind"])
        ranked.append(c)
    ranked.sort(key=lambda c: -c["score"])
    stats["candidates"] = len(ranked)
    top_n = int(cfg.get("calls", {}).get("card", {}).get("top_n_per_run", 5))
    top, rest = ranked[:top_n], ranked[top_n:]
    for c in rest:
        pending.setdefault(c["story"]["id"], now.isoformat())

    # 5. gated web search: only stories that passed everything but lack a 2nd credible source
    for c in top:
        if c["kind"] == "new" and not outlets.has_credible_second_source(c["items"]):
            v = verify(ctx.llm, store, c["story"], cfg, day, ctx.lane)
            if v is not None:
                c["verify"] = v
                stats["searched"] += 1

    if budget_exhausted(ctx, "before cards", stats):
        store.kv_set("pending", pending)
        store.commit()
        return stats

    # 6. Sonnet cards for the top N, then post + feed
    written = cardmod.write_cards(ctx.llm, top, cfg)
    if budget_exhausted(ctx, "card call", stats):
        written = []      # never post Python-only fallback cards once the cap is hit
        for c in top:
            pending.setdefault(c["story"]["id"], now.isoformat())
    for c in written:
        st, card = c["story"], c["card"]
        pending.pop(st["id"], None)
        for f in st["facts"]:
            f["new"] = False
        stats["carded"] += 1
        if not card["keep"]:
            st["killed"] = card.get("kill", "")
            stats["killed"] += 1
            store.save_story(st)
            continue
        thread = st.get("slack_ts") if c["kind"] == "update" else None
        ts = slack.post(c, cfg, now, thread_ts=thread)
        if ts:
            m = store.kv_get("slack_ts_to_story", {})
            m[ts] = st["id"]
            store.kv_set("slack_ts_to_story", m)
            if c["kind"] == "new":
                st["slack_ts"] = ts
        row = _feed_row(c, now, outlets, "updated" if c["kind"] == "update" else "new")
        ctx.feed.write(row)
        st["feed_rev"] = row["rev"]
        if c["kind"] == "new" or not st.get("card"):
            st["card"] = card
            st["posted_at"] = now.isoformat()
        else:
            st["card"] = {**(st.get("card") or {}), "last_update": card["what_happened"]}
            store.incr(day, f"updates:{st['id']}")
            stats["updates"] += 1
        st["posts"] = st.get("posts", 0) + 1
        st["last_post_at"] = now.isoformat()
        st["state"] = "posted"
        st["mw_status"] = c["mw"]["status"]
        st.pop("_new_dev_since_mw", None)
        store.save_story(st)
        stats["posted"] += 1
    store.kv_set("pending", pending)
    store.commit()
    stats["usd_today"] = round(ctx.ledger.spent(ctx.llm.app), 4)
    return stats
