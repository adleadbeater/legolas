import json
from datetime import datetime, timedelta, timezone

import pytest

from scout import extract, websearch
from scout.config import load_yaml
from scout.llm import MockBackend

from legolas2 import card, changes
from legolas2.__main__ import MOCK_RESPONDERS, build_ctx
from legolas2.card import enforce_title, protected_fact
from legolas2.pipeline import run_once

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def item(guid, title, src, mins=0, text=""):
    return {"guid": guid, "title": title, "url": f"https://example.com/{guid}", "source_name": src,
            "published_dt": T0 + timedelta(minutes=mins), "text": text or title, "via": "rss"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_STATE_DIR", str(tmp_path))
    cfg = load_yaml("config/legolas-v2.yaml")
    cfg["slack"]["post_enabled"] = False
    t = {"now": T0}
    c = build_ctx(cfg, MockBackend(MOCK_RESPONDERS, searches_per_call=1), clock=lambda: t["now"])
    c._t = t
    return c


def test_protected_fact_keeps_names_and_numbers():
    orig = "Keanu Reeves replaces Alan Ritchson in Netflix's Maelstrom."
    assert protected_fact(orig, "A big star replaces another in a thriller.", ["Keanu Reeves", "Maelstrom"]) == orig
    assert protected_fact(orig, "", ["Keanu Reeves"]) == orig
    ok = "Keanu Reeves will replace Alan Ritchson in Maelstrom (Netflix)."
    assert protected_fact(orig, ok, ["Keanu Reeves", "Alan Ritchson", "Maelstrom"]) == ok
    num = "Clayface earned $24M over the weekend."
    assert protected_fact(num, "Clayface had a strong weekend.", ["Clayface"]) == num


def test_title_guardrails():
    t = enforce_title("Robert Pattinson Officially Leads the James Bond Race", rumour=True, fallback="x")
    assert "Officially" not in t
    assert len(enforce_title("word " * 40, False, "x")) <= 95


def test_cost_scales_with_new_items_not_runs(ctx):
    arts = [item("a", "Keanu Reeves to Star in Netflix Action Movie 'Maelstrom' After Alan Ritchson Drops Out", "Variety"),
            item("b", "Alan Ritchson Exits Netflix's 'Maelstrom', Keanu Reeves Steps In", "Deadline", 10),
            item("c", "Keanu Reeves Replaces Alan Ritchson in 'Maelstrom'", "The Wrap", 20)]
    ctx._t["now"] = T0 + timedelta(minutes=30)
    s1 = run_once(ctx, ctx._t["now"], arts)
    calls_after_1 = len([r for r in ctx.ledger.rows() if "event" not in r])
    assert s1["extracted"] == 3 and calls_after_1 >= 1
    for k in range(1, 5):   # four more runs, same feed contents, no new items
        ctx._t["now"] = T0 + timedelta(minutes=30 + 15 * k)
        s = run_once(ctx, ctx._t["now"], arts)
        assert s["new"] == 0 and s["extracted"] == 0
    assert len([r for r in ctx.ledger.rows() if "event" not in r]) == calls_after_1


def test_one_story_one_row_then_update_for_new_development(ctx):
    a = [item("a", "'Clayface' Earns $3.5M in Thursday Previews", "Variety"),
         item("b", "Clayface Box Office: $3.5 Million Thursday Previews for DC Horror", "Deadline", 5),
         item("c", "'Clayface' Box Office Thursday Previews Hit $3.5M", "The Wrap", 8)]
    ctx._t["now"] = T0 + timedelta(minutes=15)
    s1 = run_once(ctx, ctx._t["now"], a)
    rows = [json.loads(l) for l in open(ctx.feed.path)]
    assert len({r["id"] for r in rows}) == 1 and s1["posted"] == 1
    # a repeat of the same fact: no new row, no card call
    ctx._t["now"] = T0 + timedelta(hours=3)
    rep = [item("d", "Clayface Thursday previews: $3.5M for the DC film", "IndieWire", 170)]
    cards_before = sum(1 for r in ctx.ledger.rows() if r.get("call") == "card")
    s2 = run_once(ctx, ctx._t["now"], rep)
    assert s2["posted"] == 0
    assert sum(1 for r in ctx.ledger.rows() if r.get("call") == "card") == cards_before
    # a genuine new development (new number) on the same story CAN post, as an update
    ctx._t["now"] = T0 + timedelta(hours=6)
    dev = [item("e", "'Clayface' Opens to $24M Weekend at the Box Office", "Variety", 350)]
    s3 = run_once(ctx, ctx._t["now"], dev)
    assert s3["updates"] == 1
    latest = ctx.feed.latest()
    assert len(latest) == 1 and list(latest.values())[0]["status"] == "updated"
    assert list(latest.values())[0]["rev"] == 2


def test_budget_exhausted_degrades_without_crashing(ctx):
    ctx.llm.cap = 0.0
    ctx._t["now"] = T0 + timedelta(minutes=15)
    s = run_once(ctx, ctx._t["now"], [item("a", "'Clayface' Earns $3.5M in Thursday Previews", "Variety"),
                                      item("b", "Clayface Box Office: $3.5 Million Thursday Previews", "Deadline", 3),
                                      item("c", "'Clayface' Box Office Thursday Previews Hit $3.5M", "The Wrap", 5)])
    assert s["extracted"] == 3                                  # heuristic fallback
    assert all("event" in r for r in ctx.ledger.rows())         # no billed calls at all
    assert any(r.get("event") == "budget_block" for r in ctx.ledger.rows())


def test_kill_switch_stops_run(ctx, monkeypatch):
    monkeypatch.setenv("SCOUT_KILL", "1")
    s = run_once(ctx, T0, [item("a", "x", "Variety")])
    assert "killed_switch" in s and s["new"] == 0


def test_websearch_daily_cap(ctx):
    st = {"key_fact": "x", "entities": [], "key_fact_source": "Variety"}
    day = ctx.ledger.today()
    n = 0
    for _ in range(10):
        if websearch.verify(ctx.llm, ctx.store, st, ctx.cfg, day, "legolas"):
            n += 1
    assert n == ctx.cfg["web_search"]["daily_cap"]


def test_no_opus_anywhere():
    cfg = load_yaml("config/legolas-v2.yaml")
    assert all("opus" not in m for m in cfg["models"].values())
