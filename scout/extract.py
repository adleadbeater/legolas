"""Entity / event extraction for NEW articles only (Haiku 5.5, batched, cached prompt).

Each article is extracted once; the result is stored on the item so no run ever sends
it again. `heuristic_extract` is the no-AI fallback (budget exhausted / kill switch)
and the mock responder used by tests and the replay.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from scout.config import REPO_ROOT
from scout.llm import LLM, BudgetBlocked, KillSwitchOff

log = logging.getLogger("scout.extract")

EVENTS = ["casting", "exit", "release_date", "renewal", "cancellation", "trailer", "first_look",
          "box_office", "streaming_ratings", "reviews_reception", "production", "development",
          "deal", "award", "death", "legal_controversy", "interview", "merch_tie_in", "feature",
          "other"]
NEWS_EVENTS = {"casting", "exit", "release_date", "renewal", "cancellation", "trailer", "first_look",
               "box_office", "streaming_ratings", "production", "development", "deal", "award",
               "death", "legal_controversy"}

SYSTEM_PATH = REPO_ROOT / "config" / "prompts" / "extract_system.md"


def system_prompt() -> str:
    return SYSTEM_PATH.read_text()


def build_user(items: List[dict]) -> str:
    lines = []
    for n, it in enumerate(items, 1):
        text = (it.get("text") or "").replace("\n", " ")
        lines.append(f"[{n}] {it['source_name']} | {it['title']} | {text}")
    return "\n".join(lines)


def _norm(rec: dict) -> dict:
    def lst(k):
        v = rec.get(k) or []
        return [str(x).strip() for x in v if str(x).strip()][:3] if isinstance(v, list) else []
    ev = rec.get("ev") if rec.get("ev") in EVENTS else "other"
    try:
        rel = max(1, min(10, int(rec.get("rel", 3))))
    except (TypeError, ValueError):
        rel = 3
    return {"ev": ev, "ti": lst("ti"), "pe": lst("pe"), "fr": lst("fr"), "pl": lst("pl"),
            "fact": str(rec.get("fact") or "").strip()[:240], "news": 1 if rec.get("news") else 0,
            "rum": 1 if rec.get("rum") else 0, "rel": rel}


def parse_output(text: str, n: int) -> Dict[int, dict]:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
            i = int(rec.get("i"))
        except Exception:
            continue
        if 1 <= i <= n:
            out[i] = _norm(rec)
    return out


# ── heuristic extractor (no AI) ──────────────────────────────────────────────
_PLATFORMS = ["Netflix", "HBO Max", "HBO", "Disney+", "Hulu", "Prime Video", "Amazon", "Apple TV",
              "Peacock", "Paramount+", "Max", "Starz", "AMC", "FX", "Showtime", "Warner Bros.",
              "Universal", "Sony", "Lionsgate", "A24", "Blumhouse", "Marvel Studios", "Lucasfilm",
              "DC Studios", "Pixar", "BBC", "CBS", "NBC", "ABC", "Fox", "The CW", "Crunchyroll"]
_FRANCHISES = ["MCU", "Marvel", "DCU", "Star Wars", "Star Trek", "Harry Potter", "Lord of the Rings",
               "James Bond", "Dune", "Alien", "Batman", "Superman", "Spider-Man", "X-Men",
               "Avengers", "Game of Thrones", "Yellowstone", "Stranger Things", "Fast & Furious",
               "Mission: Impossible", "Jurassic", "Transformers", "Godzilla", "Pokemon", "Scream",
               "Halloween", "Predator", "Avatar", "The Walking Dead", "Doctor Who"]
_EV_RULES = [
    ("death", r"\b(dies|dead at|has died|death of|passes away|rip)\b"),
    ("cancellation", r"\b(cancel(?:led|ed|s)?|axed|scrapped|won't return|ending with)\b"),
    ("renewal", r"\b(renew(?:ed|s|al)?|picked up for|returning for season)\b"),
    ("exit", r"\b(exits?|exited|leaves|leaving|drops out|departs?|steps down|quits?)\b"),
    ("casting", r"\b(cast(?:s|ing)?|joins?|joined|to star|will star|to play|tapped|recast|replac\w+|lands role)\b"),
    ("trailer", r"\b(trailer|teaser)\b"),
    ("first_look", r"\b(first look|first-look|new image|photos?)\b"),
    ("box_office", r"(box office|\$\d+(?:\.\d+)?\s?(?:m|million|b|billion)|opening weekend|thursday previews)"),
    ("streaming_ratings", r"\b(viewers|minutes watched|top 10|most-watched|nielsen|ratings)\b"),
    ("release_date", r"\b(release date|premiere date|sets? .*(?:date|premiere)|dated|delayed|moved to|arrives on)\b"),
    ("reviews_reception", r"\b(review|rotten tomatoes|reactions?|critics)\b"),
    ("production", r"\b(filming|wraps|production begins|starts shooting|begins production)\b"),
    ("development", r"\b(in development|in the works|greenlit|green-lit|announced|developing|adaptation|sequel|spinoff|spin-off|reboot)\b"),
    ("deal", r"\b(acquires?|acquisition|deal|buys)\b"),
    ("award", r"\b(oscars?|emmys?|golden globes?|nominat\w+|wins?)\b"),
    ("legal_controversy", r"\b(lawsuit|sues|sued|accus\w+|controversy|backlash|arrest\w*)\b"),
    ("merch_tie_in", r"\b(lego|funko|toy|collectible|set|shop|store|auction)\b"),
    ("interview", r"\b(says|talks|reveals|opens up|recalls|teases|explains|addresses)\b"),
]
_SPEC = re.compile(r"\b(could|might|may|fans (?:think|believe)|theory|why|should|rumou?r|reportedly|eyes?|eyed|in talks|frontrunner|\?)", re.I)
_QUOTED = re.compile(r"[‘’“”'\"]([^‘’“”'\"]{3,50})[‘’“”'\"]")
_CAPS = re.compile(r"\b([A-Z][a-zA-Z\.\-']+(?:\s+(?:of|the|and|&)?\s*[A-Z][a-zA-Z\.\-']+){1,3})\b")
_STOPCAP = {"The", "A", "An", "New", "First", "Official", "Exclusive", "Report", "Watch", "Season",
            "Episode", "Trailer", "Review", "Box Office", "Netflix", "Why", "How", "What", "Who"}


def heuristic_extract(item: dict) -> dict:
    title = item.get("title", "")
    blob = title + " " + (item.get("text") or "")[:200]
    low = blob.lower()
    ev = "other"
    for name, pat in _EV_RULES:
        if re.search(pat, low):
            ev = name
            break
    from scout.filters import LISTICLE_TITLE_RE
    if LISTICLE_TITLE_RE.search(title):
        ev = "feature"
    ti = [m.strip() for m in _QUOTED.findall(title)][:3]
    pl = [p for p in _PLATFORMS if re.search(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])", title)][:3]
    fr = [f for f in _FRANCHISES if re.search(r"(?<![A-Za-z])" + re.escape(f) + r"(?![A-Za-z])", title)][:3]
    pe = []
    for m in _CAPS.findall(title):
        words = m.split()
        if words[0] in _STOPCAP or m in pl or m in fr or any(m in t for t in ti):
            continue
        if 2 <= len(words) <= 3:
            pe.append(m)
    pe = pe[:3]
    rum = 1 if _SPEC.search(title) else 0
    news = 1 if ev in NEWS_EVENTS and not re.search(r"\?|\bcould\b|\bmight\b|\btheory\b", title, re.I) else 0
    rel = 3
    if news:
        rel = 6 + (1 if (ti or fr) and pe else 0) + (1 if item.get("source_tier", 3) == 1 else 0)
        if ev in ("death", "cancellation", "casting", "box_office", "trailer"):
            rel += 0
        if not (ti or fr or pe):
            rel = 4
    return {"ev": ev, "ti": ti, "pe": pe, "fr": fr, "pl": pl, "fact": title[:200],
            "news": news, "rum": rum, "rel": min(rel, 9), "heuristic": True}


def mock_responder(label: str, params: dict) -> str:
    """Mock for the extract call: re-parse the numbered articles and answer heuristically."""
    user = params["messages"][0]["content"]
    out = []
    for line in user.splitlines():
        m = re.match(r"\[(\d+)\] (.*?) \| (.*?) \| (.*)$", line)
        if not m:
            continue
        rec = heuristic_extract({"title": m.group(3), "text": m.group(4), "source_tier": 2})
        rec.pop("heuristic", None)
        # the real fact line is a rewritten sentence, ~same length as the headline
        out.append(json.dumps({"i": int(m.group(1)), **rec}, separators=(",", ":")))
    return "\n".join(out)


def extract_items(llm: Optional[LLM], items: List[dict], cfg: dict) -> Dict[str, dict]:
    """Returns guid -> extraction for the given (new) items. Falls back to the heuristic
    extractor for any batch the model can't do (budget / kill switch / bad output)."""
    calls = cfg.get("calls", {}).get("extract", {})
    model = cfg.get("models", {}).get("extract", "claude-haiku-5-5")
    bs = int(calls.get("batch_size", 25))
    per_item = int(calls.get("max_tokens_per_item", 90))
    out: Dict[str, dict] = {}
    sysp = system_prompt()
    for start in range(0, len(items), bs):
        batch = items[start:start + bs]
        parsed: Dict[int, dict] = {}
        if llm is not None:
            try:
                res = llm.call("extract", model=model, system=sysp, user=build_user(batch),
                               max_tokens=per_item * len(batch) + 20,
                               effort=calls.get("effort", "low"),
                               thinking=calls.get("thinking", "disabled"))
                parsed = parse_output(res.text, len(batch))
            except (BudgetBlocked, KillSwitchOff) as e:
                log.warning("extract: %s - heuristic fallback", e)
                llm = None
            except Exception as e:  # API error: degrade, don't crash the run
                log.warning("extract call failed (%s) - heuristic fallback for batch", e)
        for n, it in enumerate(batch, 1):
            out[it["guid"]] = parsed.get(n) or heuristic_extract(it)
    return out
