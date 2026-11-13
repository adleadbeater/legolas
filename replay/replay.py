"""Replay harness: run Legolas V2 over Legolas's own cached articles with a mocked (or
recorded) model client, and compare $/day against today's design.

  python replay/build_corpus.py . 2026-11-08T23:00:00Z replay/out/corpus.json
  python replay/replay.py --corpus replay/out/corpus.json --days 2026-11-09,2026-11-12

The corpus is the union of `article_cache` across the committed history of
data/learnings.json (each commit holds a 6-hour window). The cache stores headlines,
not summaries, so each article gets a stand-in excerpt of --text-chars characters
(default 450, the measured median of summary + first paragraph is ~300-450) so input
token counts are not understated.

Token counts with the mock client are ESTIMATES from text length (~3.3 chars/token);
prompt caching is simulated with the real TTL rules and the 512-token minimum. With a
recorded client (--recorded file from a live run) the recorded usage is used.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.config import load_yaml  # noqa: E402
from scout.filters import keep  # noqa: E402
from scout.ledger import Ledger, Pricing, Usage, summarize  # noqa: E402
from scout.llm import MockBackend, RecordedBackend  # noqa: E402
from scout.textutil import est_tokens, title_tokens  # noqa: E402

from legolas2.__main__ import MOCK_RESPONDERS, build_ctx  # noqa: E402
from legolas2.pipeline import run_once  # noqa: E402

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent


def load_corpus(path: Path, text_chars: int, feed_names: set):
    d = json.loads(Path(path).read_text())
    arts = []
    for a in d["articles"]:
        dt = datetime.fromisoformat(a["published_dt"])
        filler = (a["title"] + ". ") * (text_chars // max(len(a["title"]) + 2, 1) + 1)
        arts.append({"guid": a["guid"], "title": a["title"], "url": a.get("url", ""),
                     "published_dt": dt, "source_name": a["source_name"],
                     "source_tier": a.get("source_tier", 2), "text": filler[:text_chars],
                     # non-feed items were Google News results; the pipeline now drops them
                     "via": "rss" if a["source_name"] in feed_names else "gnews"})
    arts.sort(key=lambda a: a["published_dt"])
    return arts, d.get("posts", [])


def run_times(day: str, cadence_min: int, start_h: int = 5, end_h: int = 22):
    d = datetime.fromisoformat(day).replace(tzinfo=ET)
    t = d.replace(hour=start_h)
    while t <= d.replace(hour=end_h):
        yield t.astimezone(timezone.utc)
        t += timedelta(minutes=cadence_min)


# ── V2 replay ────────────────────────────────────────────────────────────────
def replay_v2(arts, days, cadence, cfg, recorded=None, keep_state=None):
    tmp = Path(keep_state or tempfile.mkdtemp(prefix="legolas-replay-"))
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    import os
    os.environ["SCOUT_STATE_DIR"] = str(tmp)
    clock = {"now": None}
    backend = RecordedBackend(Path(recorded), MOCK_RESPONDERS) if recorded else MockBackend(MOCK_RESPONDERS, searches_per_call=1)
    cfg = json.loads(json.dumps(cfg))
    cfg["slack"]["post_enabled"] = False
    cfg["budget"]["daily_usd_cap"] = 1000.0  # measure; caps are tested separately
    ctx = build_ctx(cfg, backend, clock=lambda: clock["now"])
    lb = cfg["fetch"]["lookback_minutes"]
    totals = defaultdict(int)
    i0 = 0
    # warm-up: the evening before the first day, so day 1 isn't an empty store
    first = datetime.fromisoformat(days[0]).replace(tzinfo=ET) - timedelta(days=1)
    all_days = [first.date().isoformat()] + days
    prev = None
    for day in all_days:
        for now in run_times(day, cadence):
            clock["now"] = now
            # feeds hold more than one interval: read since the previous run (overnight
            # gap included), never less than the configured lookback
            start = min(now - timedelta(minutes=lb), prev or now)
            prev = now
            window = [a for a in arts if start < a["published_dt"] <= now]
            st = run_once(ctx, now, window)
            if day in days:
                for k, v in st.items():
                    if isinstance(v, (int, float)) and k != "usd_today":
                        totals[k] += v
                totals["runs"] += 1
    rows = [r for r in ctx.ledger.rows() if r.get("day") in days]
    feed_rows = [json.loads(l) for l in open(tmp / "story-feed.jsonl")] if (tmp / "story-feed.jsonl").exists() else []
    return rows, dict(totals), feed_rows, tmp


# ── today's design (legolas.py) cost simulation ───────────────────────────────
def _old_prompt_chars():
    src = (ROOT / "legolas.py").read_text().splitlines()
    seg = lambda a, b: len("\n".join(src[a - 1:b]))
    return {"call1_static": seg(1122, 1178), "call2_system": seg(1369, 1459), "aragorn_static": seg(1972, 2029)}


_STOP = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "new", "big",
         "best", "top", "first", "last", "show", "film", "movie", "series", "season", "star", "netflix"}


def _kw(t):
    k = set()
    for q in re.findall(r"[‘’“”']([^‘’“”']{3,40})[‘’“”']", t):
        k.add("T:" + q.lower())
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", t):
        if not any(w in _STOP for w in m.group(1).lower().split()):
            k.add(m.group(1).lower())
    for w in re.findall(r"\b[A-Z][a-z]{4,}\b", t):
        if w.lower() not in _STOP:
            k.add(w.lower())
    return k


def _old_clusters(items):
    n = len(items)
    par = list(range(n))
    kws = [_kw(i["title"]) for i in items]

    def f(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    idx = defaultdict(list)
    for i, k in enumerate(kws):
        for w in k:
            if w.startswith("T:") or " " in w:
                idx[w].append(i)
    for w, ids in idx.items():
        for j in ids[1:]:
            par[f(j)] = f(ids[0])
    groups = defaultdict(list)
    for i in range(n):
        groups[f(i)].append(items[i])
    return list(groups.values())


def simulate_old(arts, days, cadence, posts_per_day, model="claude-sonnet-4-6"):
    """Cost model of legolas.py per run: items from the last 60 min that weren't posted
    (its seen-set only marks POSTED articles, so every run re-sends the last hour), +6h
    cache for clustering -> Call 1 (merge) + Call 2 (assess, 1 block per cluster, 20K max)
    + Aragorn for the posts. Prompt sizes measured from legolas.py's own templates."""
    pc = _old_prompt_chars()
    pricing = Pricing()
    usd = 0.0
    tok = defaultdict(int)
    runs = 0
    for day in days:
        rts = list(run_times(day, cadence, 5, 22))
        posts_per_run = posts_per_day / max(len(rts), 1)
        for now in rts:
            runs += 1
            new = [a for a in arts if now - timedelta(minutes=60) < a["published_dt"] <= now and keep(a)]
            cache = [a for a in arts if now - timedelta(hours=6) < a["published_dt"] <= now - timedelta(minutes=60)]
            cl = [c for c in _old_clusters(new + cache) if any(x in new for x in c)]
            if not cl:
                continue
            # Call 1: static + 3 headlines per cluster + 40 recently posted (headline + 3 titles)
            c1_in = est_tokens("x" * pc["call1_static"]) + sum(est_tokens(" | ".join(x["title"][:70] for x in c[:3])) + 3 for c in cl) + 40 * 85
            c1_out = 40
            # Call 2: system (cached 5m; runs >5 min apart so it's re-written each run) + per-cluster block
            blocks = sum(30 + sum(est_tokens(f"  [T1] {x['source_name']:<22} 999m ago | {x['title']}") for x in c) for c in cl)
            c2_sys = est_tokens("x" * pc["call2_system"])
            chunks = (len(cl) + 39) // 40
            c2_out = 85 * len(cl)
            # Aragorn
            ar_in = est_tokens("x" * pc["aragorn_static"]) + int(posts_per_run * 160)
            ar_out = int(posts_per_run * 70)
            u = [Usage(input_tokens=c1_in, output_tokens=c1_out),
                 Usage(input_tokens=blocks, output_tokens=c2_out, cache_write_tokens=c2_sys,
                       cache_read_tokens=c2_sys * (chunks - 1)),
                 Usage(input_tokens=ar_in, output_tokens=ar_out)]
            for x in u:
                usd += pricing.cost(model, x)
                tok["input"] += x.input_tokens + x.cache_write_tokens + x.cache_read_tokens
                tok["output"] += x.output_tokens
    return {"usd_per_day": usd / len(days), "runs_per_day": runs / len(days),
            "input_tokens_per_day": tok["input"] / len(days), "output_tokens_per_day": tok["output"] / len(days)}


def report_v2(rows, totals, days, label):
    s = summarize(rows)
    nd = len(days)
    by = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if "event" in r:
            continue
        k = f"{r['call']}:{r['model']}"
        for f in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "web_searches", "usd"):
            by[k][f] += r.get(f, 0)
        by[k]["calls"] += 1
    usd = sum(d["usd"] for d in s.values()) / nd
    out = {"label": label, "usd_per_day": round(usd, 4),
           "per_day_by_call": {k: {f: round(v / nd, 4 if f == "usd" else 1) for f, v in d.items()} for k, d in by.items()},
           "per_day": {k: round(v / nd, 1) for k, v in totals.items()},
           "daily_usd": {d: round(v["usd"], 4) for d, v in sorted(s.items())}}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(ROOT / "replay/out/corpus.json"))
    ap.add_argument("--days", default="2026-11-09,2026-11-10,2026-11-11,2026-11-12")
    ap.add_argument("--cadences", default="30,15,5")
    ap.add_argument("--text-chars", type=int, default=450)
    ap.add_argument("--recorded")
    ap.add_argument("--out", default=str(ROOT / "replay/out/replay-report.json"))
    a = ap.parse_args()
    cfg = load_yaml("config/legolas-v2.yaml")
    feeds = load_yaml("config/feeds.yaml")
    feed_names = {e["name"] for t in ("tier1", "tier2") for e in feeds.get(t, [])}
    arts, posts = load_corpus(Path(a.corpus), a.text_chars, feed_names)
    days = a.days.split(",")
    posts_per_day = sum(1 for p in posts if datetime.fromisoformat(p["posted_at"]).astimezone(ET).date().isoformat() in days) / len(days)
    in_days = [x for x in arts if x["published_dt"].astimezone(ET).date().isoformat() in days]
    report = {"corpus": {"articles_in_days_per_day": round(len(in_days) / len(days), 1),
                         "after_filter_per_day": round(sum(1 for x in in_days if keep(x)) / len(days), 1),
                         "old_design_posts_per_day": round(posts_per_day, 1), "days": days,
                         "text_chars_assumed": a.text_chars},
              "today_design": {}, "v2": {}}
    for cad in (30, 15):
        for model in ("claude-sonnet-4-6", "claude-sonnet-5-5"):
            r = simulate_old(arts, days, cad, posts_per_day, model)
            report["today_design"][f"{cad}min:{model}"] = {k: round(v, 3) for k, v in r.items()}
            print(f"today's design @{cad}min on {model}: ${r['usd_per_day']:.2f}/day ({r['runs_per_day']:.0f} runs/day)")
    for cad in [int(c) for c in a.cadences.split(",")]:
        rows, totals, feed_rows, tmp = replay_v2(arts, days, cad, cfg, a.recorded,
                                                 keep_state=str(ROOT / f"replay/out/state-{cad}min"))
        rep = report_v2(rows, totals, days, f"v2@{cad}min")
        rep["feed_rows_per_day"] = round(len([r for r in feed_rows if r["updated_at"][:10] >= days[0]]) / len(days), 1)
        report["v2"][f"{cad}min"] = rep
        print(f"V2 @{cad}min: ${rep['usd_per_day']:.3f}/day  {json.dumps(rep['per_day'])}")
        for k, v in rep["per_day_by_call"].items():
            print(f"   {k:<32} calls={v['calls']:<6} in={v['input_tokens']:<9} out={v['output_tokens']:<8} "
                  f"cw={v['cache_write_tokens']:<8} cr={v['cache_read_tokens']:<9} ws={v['web_searches']:<4} ${v['usd']:.4f}")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=2))
    print("report:", a.out)


if __name__ == "__main__":
    main()
