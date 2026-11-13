import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from scout import extract, websearch
from scout.config import load_yaml
from scout.config import REPO_ROOT as ROOT
from scout.llm import BudgetBlocked, MockBackend

from legolas2 import card, changes
from legolas2.__main__ import MOCK_RESPONDERS, build_ctx
from legolas2.card import enforce_title, protected_fact
from legolas2 import pipeline as pipeline_mod
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


CLAYFACE = [("a", "'Clayface' Earns $3.5M in Thursday Previews", "Variety", 0),
            ("b", "Clayface Box Office: $3.5 Million Thursday Previews", "Deadline", 3),
            ("c", "'Clayface' Box Office Thursday Previews Hit $3.5M", "The Wrap", 5)]


def _posting_on(ctx, monkeypatch):
    """Turn posting on but capture it, so a test can prove nothing went to Slack."""
    sent = []
    ctx.cfg["slack"]["post_enabled"] = True
    monkeypatch.setattr(pipeline_mod.slack, "post", lambda c, cfg, now, thread_ts=None: sent.append(c) or "ts")
    return sent


def test_daily_cap_is_one_dollar_and_stop_means_post_nothing():
    cfg = load_yaml("config/legolas-v2.yaml")
    assert cfg["budget"]["daily_usd_cap"] == 1.0
    assert cfg["budget"]["on_exhausted"] == "skip"


def test_cap_already_spent_stops_run_and_posts_nothing(ctx, monkeypatch):
    sent = _posting_on(ctx, monkeypatch)
    ctx.llm.cap = 0.0
    ctx._t["now"] = T0 + timedelta(minutes=15)
    s = run_once(ctx, ctx._t["now"], [item(g, t, src, m) for g, t, src, m in CLAYFACE])
    assert s["budget_stop"] == "run start" and s["new"] == 0 and s["posted"] == 0
    assert sent == [] and not ctx.feed.path.exists()
    assert all("event" in r for r in ctx.ledger.rows())         # no billed calls at all
    assert [r["event"] for r in ctx.ledger.rows()] == ["budget_stop"]


def test_cap_hit_mid_run_stops_and_posts_nothing(ctx, monkeypatch):
    """Extraction fits under the cap, the card call does not: no Python-only cards go out."""
    sent = _posting_on(ctx, monkeypatch)
    real_call = ctx.llm.call

    def call(label, **kw):
        if label == "card":   # simulate the pre-call estimate crossing the cap
            ctx.llm.budget_hit = True
            ctx.ledger.record_event(app="legolas", lane="legolas", event="budget_block", detail="test")
            raise BudgetBlocked("legolas daily cap $1.00 reached")
        return real_call(label, **kw)

    monkeypatch.setattr(ctx.llm, "call", call)
    ctx._t["now"] = T0 + timedelta(minutes=15)
    s = run_once(ctx, ctx._t["now"], [item(g, t, src, m) for g, t, src, m in CLAYFACE])
    assert s["extracted"] == 3 and s["candidates"] >= 1
    assert s["budget_stop"] and s["posted"] == 0 and s["carded"] == 0
    assert sent == [] and not ctx.feed.path.exists()
    ev = [r.get("event") for r in ctx.ledger.rows() if "event" in r]
    assert "budget_block" in ev and ev.count("budget_stop") == 1
    # the story is kept pending, and the next day (fresh budget) it can post
    assert ctx.store.kv_get("pending", {})


def test_extraction_blocked_by_cap_stops_before_posting(ctx, monkeypatch):
    sent = _posting_on(ctx, monkeypatch)
    ctx.llm.cap = 0.0000001   # nothing fits, but remaining() > 0 at run start
    ctx._t["now"] = T0 + timedelta(minutes=15)
    s = run_once(ctx, ctx._t["now"], [item(g, t, src, m) for g, t, src, m in CLAYFACE])
    assert s["budget_stop"] == "after extraction" and s["extracted"] == 3   # heuristic only
    assert s["posted"] == 0 and sent == []
    assert any(r.get("event") == "budget_block" for r in ctx.ledger.rows())
    # those articles are in the seen-set: never re-billed on the next run
    s2 = run_once(ctx, ctx._t["now"] + timedelta(minutes=15), [item(g, t, src, m) for g, t, src, m in CLAYFACE])
    assert s2.get("new", 0) == 0


# ── Google News removed (13 Nov) ─────────────────────────────────────────────
def test_google_news_is_gone():
    import scout.fetch as fetch
    cfg = load_yaml("config/legolas-v2.yaml")
    assert "google_news" not in cfg["fetch"]
    assert "google_news" not in load_yaml("config/outlets.yaml")
    assert not hasattr(fetch, "fetch_google_news")
    for f in ("legolas.py", "scout/fetch.py", "legolas2/__main__.py", "legolas2/pipeline.py"):
        assert "news.google.com" not in (ROOT / f).read_text(), f


def test_items_not_from_our_feeds_are_dropped(ctx):
    ctx._t["now"] = T0 + timedelta(minutes=15)
    stray = {**item("g", "Keanu Reeves boards Maelstrom", "Variety"), "via": "gnews"}
    s = run_once(ctx, ctx._t["now"], [stray])
    assert s["new"] == 0


# ── title rules come from a secret, never the public repo ────────────────────
RULES = "PRIVATE RULES: star-led titles 46% hit"


def _clear_rules_env(monkeypatch):
    for k in ("LEGOLAS_TITLE_RULES", "LEGOLAS_TITLE_RULES_B64", "LEGOLAS_TITLE_RULES_PATH"):
        monkeypatch.delenv(k, raising=False)


def test_title_rules_from_secret_env(monkeypatch, tmp_path):
    import base64
    from scout.private_text import load_title_rules
    _clear_rules_env(monkeypatch)
    public = load_title_rules()
    assert public.startswith("MovieWeb title rules (PUBLIC PLACEHOLDER")
    monkeypatch.setenv("LEGOLAS_TITLE_RULES_B64", base64.b64encode(RULES.encode()).decode())
    assert load_title_rules() == RULES
    # base64 pasted with line breaks still decodes
    b = base64.encodebytes(RULES.encode()).decode()
    monkeypatch.setenv("LEGOLAS_TITLE_RULES_B64", b)
    assert load_title_rules() == RULES
    monkeypatch.setenv("LEGOLAS_TITLE_RULES", "plain text wins")
    assert load_title_rules() == "plain text wins"
    _clear_rules_env(monkeypatch)
    p = tmp_path / "rules.md"
    p.write_text(RULES)
    monkeypatch.setenv("LEGOLAS_TITLE_RULES_PATH", str(p))
    assert load_title_rules() == RULES
    _clear_rules_env(monkeypatch)
    monkeypatch.setenv("LEGOLAS_TITLE_RULES_B64", "not base64 !!!")
    assert load_title_rules() == public          # bad secret: placeholder, no crash


def test_card_and_aragorn_prompts_use_the_secret(monkeypatch):
    import base64
    _clear_rules_env(monkeypatch)
    monkeypatch.setenv("LEGOLAS_TITLE_RULES_B64", base64.b64encode(RULES.encode()).decode())
    cfg = load_yaml("config/legolas-v2.yaml")
    assert RULES in card.system_prompt(cfg)
    src = (ROOT / "legolas.py").read_text()
    assert "{_aragorn_rules()}" in src


def test_no_mw_performance_data_in_public_files():
    pat = re.compile(r"\d+% hit|hit rate|\d[\d,]* sessions vs", re.I)
    files = [ROOT / "legolas.py", ROOT / "config/prompts/title_rules_public.md",
             ROOT / "config/prompts/card_system.md", ROOT / "config/legolas-v2.yaml"]
    for f in files:
        assert not pat.search(f.read_text()), f


# ── approved workflow ────────────────────────────────────────────────────────
def test_workflow_is_the_approved_one():
    wf = load_yaml(".github/workflows/legolas.yml")
    text = (ROOT / ".github/workflows/legolas.yml").read_text()
    crons = [c["cron"] for c in wf[True]["schedule"]]   # PyYAML reads the key `on` as True
    assert crons and all(c.startswith("*/15 ") for c in crons)
    assert "python -m legolas2 --post" in text and "python legolas.py" not in text
    assert "actions/cache/restore@" in text and "actions/cache/save@" in text
    assert "git push" not in text and wf["permissions"]["contents"] == "read"
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert "LEGOLAS_TITLE_RULES_B64: ${{ secrets.LEGOLAS_TITLE_RULES_B64 }}" in text
    for job in wf["jobs"].values():                       # secrets only via env, never inline in run:
        for step in job.get("steps", []):
            assert "secrets." not in step.get("run", "")


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
