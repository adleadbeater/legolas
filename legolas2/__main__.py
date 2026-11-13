"""Run Legolas V2 once:  python -m legolas2 [--dry-run] [--mock] [--post]

--mock     never call Claude (heuristic/mock responders); for local checks
--dry-run  force Slack posting off (the default config already has it off)
--post     turn Slack posting on for this run (the GitHub Actions workflow uses this;
           local runs stay silent unless you ask)
Secrets come from env only: ANTHROPIC_API_KEY, SLACK_WEBHOOK, SLACK_BOT_TOKEN,
LEGOLAS_POST_CHANNEL, LEGOLAS_FEEDBACK_CHANNEL, GOOGLE_APPLICATION_CREDENTIALS,
LEGOLAS_TITLE_RULES / LEGOLAS_TITLE_RULES_B64 / LEGOLAS_TITLE_RULES_PATH.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from scout import extract, websearch
from scout.config import get, load_yaml, state_dir
from scout.feed import FeedWriter
from scout.fetch import fetch_feeds, fetch_mw_articles
from scout.killswitch import KillSwitch
from scout.ledger import Ledger, format_summary
from scout.llm import LLM, AnthropicBackend, MockBackend
from scout.outlets import Outlets
from scout.store import Store
from scout.tags import DEFAULT_STOPWORD_TAGS

from legolas2 import card, changes, feedback, sheets
from legolas2.pipeline import Ctx, run_once

MOCK_RESPONDERS = {"extract": extract.mock_responder, "card": card.mock_responder,
                   "change_detect": changes.mock_responder, "verify": websearch.mock_responder,
                   "feedback": lambda l, p: '{"action":"note","reason":null,"note":""}'}


def build_ctx(cfg: dict, backend, clock=None) -> Ctx:
    sd = state_dir(cfg)
    feeds = load_yaml("config/feeds.yaml")
    feed_tiers = {e["name"]: 1 for e in feeds.get("tier1", [])}
    feed_tiers.update({e["name"]: 2 for e in feeds.get("tier2", [])})
    outlets = Outlets(feed_tiers=feed_tiers)
    ledger = Ledger(sd / "cost-ledger.jsonl", clock=clock, tz=get(cfg, "budget.timezone", "America/New_York"))
    ks = KillSwitch(sd / "killswitch.json", cfg.get("app", "legolas"), cfg.get("enabled", True))
    llm = LLM(cfg.get("app", "legolas"), backend, ledger, ks, float(get(cfg, "budget.daily_usd_cap", 3.0)),
              cache_ttl=get(cfg, "caching.ttl", "1h"), run_id=uuid.uuid4().hex[:8])
    v1 = load_yaml("config/agent-config.yaml")
    return Ctx(cfg=cfg, store=Store(sd / "stories.db"), ledger=ledger, llm=llm, outlets=outlets,
               feed=FeedWriter(sd / "story-feed.jsonl"),
               feed_names={outlets.canonical(n) for n in feed_tiers},
               generic_tags=v1.get("generic_tag_blocklist", []),
               baity={outlets.canonical(n) for n in v1.get("legolas_pick_excluded_sources", [])})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--config", default="config/legolas-v2.yaml")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    cfg = load_yaml(a.config)
    if a.post and not a.dry_run:
        cfg.setdefault("slack", {})["post_enabled"] = True
    if a.dry_run:
        cfg.setdefault("slack", {})["post_enabled"] = False
    use_mock = a.mock or not os.environ.get("ANTHROPIC_API_KEY")
    if use_mock:
        logging.warning("No ANTHROPIC_API_KEY (or --mock): using the mock model client")
    backend = MockBackend(MOCK_RESPONDERS) if use_mock else AnthropicBackend()
    ctx = build_ctx(cfg, backend)
    now = datetime.now(timezone.utc)
    if not ctx.llm.ks.is_on():
        logging.warning("kill switch: %s", ctx.llm.ks.reason_off())
        return
    if ctx.llm.remaining() <= 0:
        spent = ctx.ledger.spent(ctx.llm.app)
        ctx.ledger.record_event(app=ctx.llm.app, lane="legolas", event="budget_stop",
                                detail=f"run start: spent={spent:.4f} cap={ctx.llm.cap}")
        logging.warning("Legolas daily cap $%.2f already spent ($%.4f) - skipping this run, nothing posted",
                        ctx.llm.cap, spent)
        print(format_summary(ctx.ledger.summary(ctx.ledger.today())))
        return
    v1 = load_yaml("config/agent-config.yaml")
    ctx.priority_tags, ctx.watched = sheets.load(v1, get(cfg, "gates.tag_sa_min", 5000),
                                                 get(cfg, "gates.tag_fr_max", 25), DEFAULT_STOPWORD_TAGS)
    fb = feedback.process(ctx.llm, ctx.store, ctx.feed, cfg, now.isoformat())
    feeds = load_yaml("config/feeds.yaml")
    srcs = {e["name"]: e["url"] for t in ("tier1", "tier2") for e in feeds.get(t, [])}
    tiers = {e["name"]: (1 if t == "tier1" else 2) for t in ("tier1", "tier2") for e in feeds.get(t, [])}
    lb = get(cfg, "fetch.lookback_minutes", 120)
    items = fetch_feeds(srcs, tiers, ctx.outlets, lb, get(cfg, "fetch.max_text_chars", 600))
    mw = fetch_mw_articles(get(cfg, "mw_check.sitemap_url"))
    stats = run_once(ctx, now, items, mw)
    ctx.store.prune(now)
    print(json.dumps({"feedback": fb, **stats}, indent=2))
    print(format_summary(ctx.ledger.summary(ctx.ledger.today())))


if __name__ == "__main__":
    main()
