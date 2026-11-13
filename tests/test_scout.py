import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.cluster import assign
from scout.extract import heuristic_extract, parse_output
from scout.feed import FeedWriter, reject, validate
from scout.killswitch import KillSwitch
from scout.ledger import Ledger, Pricing, Usage
from scout.llm import LLM, BudgetBlocked, KillSwitchOff, MockBackend
from scout.mwcheck import check
from scout.outlets import Outlets
from scout.store import Store
from scout.tags import match_tags

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc)


def item(guid, title, src="Variety", tier=1, mins=0, text=""):
    return {"guid": guid, "title": title, "url": f"https://x/{guid}", "source_name": src,
            "source_tier": tier, "published_dt": T0 + timedelta(minutes=mins), "text": text}


def ext(ev, ti=(), pe=(), fr=(), pl=(), fact="", rel=7, news=1):
    return {"ev": ev, "ti": list(ti), "pe": list(pe), "fr": list(fr), "pl": list(pl),
            "fact": fact, "news": news, "rum": 0, "rel": rel}


# ── pricing / ledger ─────────────────────────────────────────────────────────
def test_pricing_haiku_sonnet_cache_batch_search():
    p = Pricing()
    assert p.cost("claude-haiku-5-5", Usage(input_tokens=1_000_000)) == pytest.approx(0.10)
    assert p.cost("claude-haiku-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(0.50)
    assert p.cost("claude-sonnet-5-5", Usage(cache_read_tokens=1_000_000)) == pytest.approx(0.10)
    assert p.cost("claude-sonnet-5-5", Usage(cache_write_tokens=1_000_000, cache_ttl="1h")) == pytest.approx(4.00)
    assert p.cost("claude-sonnet-5-5", Usage(cache_write_tokens=1_000_000, cache_ttl="5m")) == pytest.approx(2.50)
    assert p.cost("claude-sonnet-5-5", Usage(output_tokens=1_000_000), batch=True) == pytest.approx(5.00)
    assert p.cost("claude-haiku-5-5", Usage(web_searches=3)) == pytest.approx(0.03)
    with pytest.raises(KeyError):
        p.cost("claude-opus-5-5", Usage())


def _llm(tmp_path, cap=1.0, responders=None, clock=None):
    led = Ledger(tmp_path / "ledger.jsonl", clock=clock or (lambda: T0))
    ks = KillSwitch(tmp_path / "killswitch.json", "legolas")
    be = MockBackend(responders or {"x": lambda l, p: '{"ok":1}'})
    return LLM("legolas", be, led, ks, cap, cache_ttl="1h"), led


def test_ledger_records_every_call_and_summary(tmp_path):
    llm, led = _llm(tmp_path)
    llm.call("x", model="claude-haiku-5-5", system="s" * 4000, user="hello", max_tokens=50)
    rows = led.rows()
    assert len(rows) == 1 and rows[0]["model"] == "claude-haiku-5-5"
    for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "usd"):
        assert k in rows[0]
    s = led.summary()
    assert s[led.today()]["apps"]["legolas"]["calls"] == 1


def test_prompt_cache_1h_reads_on_second_call(tmp_path):
    t = {"now": T0}
    llm, led = _llm(tmp_path, clock=lambda: t["now"])
    big = "rules " * 800   # > 512 tokens: cacheable
    llm.call("x", model="claude-sonnet-5-5", system=big, user="a", max_tokens=20)
    t["now"] = T0 + timedelta(minutes=15)   # next run, past the 5-minute TTL
    llm.call("x", model="claude-sonnet-5-5", system=big, user="b", max_tokens=20)
    r1, r2 = led.rows()
    assert r1["cache_write_tokens"] > 0 and r1["cache_read_tokens"] == 0
    assert r2["cache_read_tokens"] > 0 and r2["cache_write_tokens"] == 0


def test_budget_cap_blocks_and_logs(tmp_path):
    llm, led = _llm(tmp_path, cap=0.0005)
    with pytest.raises(BudgetBlocked):
        llm.call("x", model="claude-sonnet-5-5", system="s", user="u" * 2000, max_tokens=1000)
    assert any(r.get("event") == "budget_block" for r in led.rows())
    assert led.spent("legolas") == 0


def test_kill_switch_env_file_and_lane(tmp_path, monkeypatch):
    llm, led = _llm(tmp_path)
    monkeypatch.setenv("LEGOLAS_KILL", "1")
    with pytest.raises(KillSwitchOff):
        llm.call("x", model="claude-haiku-5-5", system="s", user="u", max_tokens=5)
    monkeypatch.delenv("LEGOLAS_KILL")
    (tmp_path / "killswitch.json").write_text(json.dumps({"lanes": {"legolas:horror": False}}))
    llm.call("x", model="claude-haiku-5-5", system="s", user="u", max_tokens=5)  # other lane fine
    with pytest.raises(KillSwitchOff):
        llm.call("x", model="claude-haiku-5-5", system="s", user="u", max_tokens=5, lane="horror")
    (tmp_path / "killswitch.json").write_text("{not json")
    assert not KillSwitch(tmp_path / "killswitch.json", "legolas").is_on()  # fails safe


def test_batch_is_half_price(tmp_path):
    llm, led = _llm(tmp_path, responders={"x": lambda l, p: "y" * 330})
    bid = llm.submit_batch("x", model="claude-sonnet-5-5", system="s", users={"a": "hi"}, max_tokens=200)
    out = llm.collect_batch(bid, "x", "claude-sonnet-5-5")
    r = led.rows()[-1]
    assert out == {"a": "y" * 330} and r["batch"] is True
    full = Pricing().cost("claude-sonnet-5-5", Usage(input_tokens=r["input_tokens"], output_tokens=r["output_tokens"]))
    assert r["usd"] == pytest.approx(full / 2)


# ── outlets ──────────────────────────────────────────────────────────────────
def test_outlet_normalisation_and_independence():
    o = Outlets(feed_tiers={"Screen Rant": 2, "Variety": 1})
    assert o.canonical("ScreenRant") == o.canonical("Screen Rant") == "Screen Rant"
    assert o.canonical("TheWrap") == o.canonical("The Wrap")
    assert o.canonical("Comic Book Resources") == "CBR"
    pmc = [item(str(i), t, s) for i, (t, s) in enumerate([
        ("Keanu Reeves boards Netflix thriller Maelstrom", "Variety"),
        ("Alan Ritchson exits Maelstrom over scheduling", "Deadline"),
        ("Netflix recasts its hurricane action movie lead", "Hollywood Reporter")])]
    assert o.weighted_sources(pmc) == pytest.approx(1.5)
    junk = [item("1", "Keanu story", "IMDb"), item("2", "Keanu story two", "TradingView")]
    assert o.weighted_sources(junk) == 0
    wire = [item(str(i), "Darth Vader lightsaber sells for record sum at auction house", s, tier=1)
            for i, s in enumerate(["Associated Press", "Reuters"])]
    assert o.weighted_sources(wire) == 1.0          # syndicated copy counts once
    assert not o.google_news_allowed("alphavilleearredores.com.br")
    assert o.google_news_allowed("Variety") and not o.google_news_allowed("MovieWeb")


# ── tags ─────────────────────────────────────────────────────────────────────
def test_tags_match_entities_not_headline_words():
    tags = {"from": {"sa": 9000, "fr": 5}, "legends": {"sa": 9000, "fr": 5},
            "keanu reeves": {"sa": 20000, "fr": 5}, "star wars": {"sa": 30000, "fr": 5}}
    assert match_tags(["Avatar: Seven Havens"], tags) == []
    got = {m["tag"] for m in match_tags(["Keanu Reeves", "Star Wars: Maul"], tags)}
    assert got == {"keanu reeves", "star wars"}


# ── extraction parsing ───────────────────────────────────────────────────────
def test_parse_output_tolerates_junk_and_clamps():
    text = 'here you go\n{"i":1,"ev":"casting","ti":["Maelstrom"],"pe":["Keanu Reeves"],"fr":[],"pl":["Netflix"],"fact":"x","news":1,"rum":0,"rel":42}\n{"i":9,"ev":"bogus"}\nnot json'
    out = parse_output(text, 2)
    assert set(out) == {1} and out[1]["rel"] == 10 and out[1]["ev"] == "casting"


# ── clustering ───────────────────────────────────────────────────────────────
def test_entity_event_clustering(tmp_path):
    st = Store(tmp_path / "s.db")
    a = item("a", "Keanu Reeves to star in Maelstrom", "Variety")
    b = item("b", "Alan Ritchson exits Maelstrom", "Deadline", mins=30)
    c = item("c", "Giant Chocolate Frog appears in London for Harry Potter", "Bleeding Cool", 2, 40)
    d = item("d", "HBO's Harry Potter casts Lily Potter", "Variety", mins=50)
    e = item("e", "LEGO reveals Hogwarts set for Harry Potter", "The Brick Fan", 2, 55)
    pairs = [(a, ext("casting", ti=["Maelstrom"], pe=["Keanu Reeves"])),
             (b, ext("exit", ti=["Maelstrom"], pe=["Alan Ritchson"])),
             (c, ext("merch_tie_in", fr=["Harry Potter"])),
             (d, ext("casting", ti=["Harry Potter"], pe=["Lily Potter"])),
             (e, ext("merch_tie_in", ti=["Harry Potter"], pe=[]))]
    for i, _ in pairs:
        st.add_items([i], T0)
    touched, created = assign(st, pairs, T0 + timedelta(hours=1))
    by_guid = {}
    for sid, s in touched.items():
        for f in s["facts"]:
            by_guid[f["guid"]] = sid
    assert by_guid["a"] == by_guid["b"]            # casting + exit on Maelstrom = one story
    assert by_guid["d"] != by_guid["e"]            # casting vs LEGO set: separate
    assert by_guid["c"] != by_guid["d"]            # franchise word alone never joins


def test_seen_set_and_title_dupes(tmp_path):
    st = Store(tmp_path / "s.db")
    a = item("a", "Same headline", "Variety")
    st.add_items([a], T0)
    assert st.unseen([a]) == []
    assert st.unseen([{**a, "guid": "a2"}]) == []   # same outlet, same headline, new guid
    assert len(st.unseen([{**a, "guid": "b", "source_name": "Deadline"}])) == 1


# ── MW check ─────────────────────────────────────────────────────────────────
def test_mw_check_statuses():
    story = {"event_type": "casting", "facts": [{"at": (T0 + timedelta(hours=5)).isoformat()}],
             "entities": [{"type": "ti", "key": "maelstrom", "name": "Maelstrom", "n": 2},
                          {"type": "pe", "key": "keanu reeves", "name": "Keanu Reeves", "n": 2}]}
    mw_cov = [{"url": "https://movieweb.com/keanu-reeves-replaces-alan-ritchson-maelstrom/",
               "title": "Keanu Reeves Officially Replaces Alan Ritchson in Netflix Thriller",
               "published_dt": T0}]
    assert check(story, mw_cov)["status"] == "covered"
    mw_other = [{"url": "https://movieweb.com/keanu-reeves-john-wick-5-box-office/",
                 "title": "Keanu Reeves Action Hit Crosses $300 Million Box Office", "published_dt": T0}]
    assert check(story, mw_other)["status"] == "follow_up"
    assert check(story, [])["status"] == "not_on_mw"


# ── feed contract ────────────────────────────────────────────────────────────
def _row(**kw):
    r = {"id": "st-1", "rev": 1, "agent": "Legolas", "title": "t", "what_happened": "w",
         "first_seen": "x", "updated_at": "x", "status": "new", "mw_status": "not_on_mw",
         "sources": [], "source_tier": 1}
    r.update(kw)
    return r


def test_feed_requires_fact_line_and_reason_is_optional(tmp_path):
    assert "what_happened is required" in validate({**_row(), "v": 1, "what_happened": " "})
    fw = FeedWriter(tmp_path / "feed.jsonl")
    fw.write(_row())
    r = reject(fw, "st-1")                   # no reason: allowed
    assert r["status"] == "rejected" and r["reject_reason"] is None and r["rev"] == 2
    r = reject(fw, "st-1", "already_on_mw", "we ran it at noon")
    assert fw.latest()["st-1"]["rev"] == 3
    with pytest.raises(ValueError):
        reject(fw, "st-1", "made_up_reason")


def test_heuristic_extract_basics():
    e = heuristic_extract({"title": "Keanu Reeves to Star in Netflix Action Movie 'Maelstrom' After Alan Ritchson Drops Out"})
    assert e["ev"] in ("casting", "exit") and "Maelstrom" in e["ti"] and "Netflix" in e["pl"]


def test_mw_check_common_word_titles_and_family():
    story = {"event_type": "streaming_ratings", "facts": [{"at": T0.isoformat()}],
             "entities": [{"type": "ti", "key": "from", "name": "From", "n": 1}]}
    mw = [{"url": "https://movieweb.com/daniel-craig-bond/", "published_dt": T0,
           "title": "Daniel Craig's Final Bond Movie Faces Defeat From Near-Perfect 95-Minute Thriller"}]
    assert check(story, mw)["status"] == "not_on_mw"
    eb = {"event_type": "box_office", "facts": [{"at": T0.isoformat()}],
          "entities": [{"type": "ti", "key": "ebenezer", "name": "Ebenezer", "n": 2},
                       {"type": "pe", "key": "johnny depp", "name": "Johnny Depp", "n": 1}]}
    mw2 = [{"url": "https://movieweb.com/ebenezer-rotten-tomatoes/", "published_dt": T0,
            "title": "Johnny Depp's 'Ebenezer' Remake Lands Rotten Tomatoes Score"}]
    assert check(eb, mw2)["status"] == "follow_up"   # MW has the film, not the box-office news
