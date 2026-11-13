"""Is a new article on an already-posted story a genuine new development?

Python decides the clear cases (a new event family, a new number); Haiku 5.5 is asked
only about the ambiguous ones (the new article introduces a name the story didn't
have). Everything else is a repeat and costs nothing. A genuine development CAN post
(update in place); repeats never do.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple

from scout.cluster import FAMILY
from scout.llm import LLM, BudgetBlocked, KillSwitchOff
from scout.textutil import fold

_NUM = re.compile(r"\$?\d[\d,\.]*\s?(?:m\b|million|b\b|billion|%)?", re.I)

SYSTEM = """You check whether new articles add a genuinely new development to a story MovieWeb's news desk has already been sent.
A new development is a new confirmed fact: a new person cast or leaving, a new date, a new number, a renewal or cancellation, an official confirmation of an earlier report. Re-reporting, reaction, analysis or the same fact in new words is NOT new.
For each numbered CASE output one line of JSON only:
{"i":<n>,"new":0|1,"fact":"<the new fact in one sentence with names and numbers, or empty>"}"""


def new_facts(story: dict) -> List[dict]:
    since = story.get("last_post_at") or ""
    return [f for f in story["facts"] if f.get("new") and f["at"] > since]


def classify(story: dict) -> Tuple[str, List[dict]]:
    """Returns ('dev'|'ambiguous'|'repeat', new_facts)."""
    nf = new_facts(story)
    if not nf:
        return "repeat", nf
    posted_text = fold(json.dumps(story.get("card") or {}) + " " +
                       " ".join(f["fact"] for f in story["facts"] if not f.get("new")))
    posted_fams = {FAMILY.get(f["ev"], "other") for f in story["facts"] if not f.get("new")}
    for f in nf:
        if FAMILY.get(f["ev"], "other") not in posted_fams and f["ev"] not in ("feature", "other", "interview"):
            return "dev", [f]
        nums = [n.strip() for n in _NUM.findall(f["fact"]) if len(n.strip()) >= 2]
        if any(fold(n) not in posted_text for n in nums):
            return "dev", [f]
    known = {e["key"] for e in story["entities"] if e["n"] > 1}
    if any(len(fold(f["fact"])) and any(w for w in re.findall(r"[A-Z][a-z]+ [A-Z][a-z]+", f["fact"])
                                       if fold(w) not in posted_text) for f in nf):
        return "ambiguous", nf
    return "repeat", nf


def resolve_ambiguous(llm: LLM, cases: List[Tuple[dict, List[dict]]], cfg: dict) -> Dict[str, dict]:
    if not cases:
        return {}
    c = cfg.get("calls", {}).get("change_detect", {})
    blocks = []
    for n, (st, nf) in enumerate(cases, 1):
        posted = (st.get("card") or {}).get("what_happened") or st["key_fact"]
        blocks.append(f"CASE {n}\nALREADY SENT: {posted}\nNEW: " + " | ".join(f["fact"] for f in nf[:4]))
    try:
        res = llm.call("change_detect", model=cfg.get("models", {}).get("change_detect", "claude-haiku-5-5"),
                       system=SYSTEM, user="\n\n".join(blocks),
                       max_tokens=int(c.get("max_tokens", 300)) + 60 * len(cases),
                       effort=c.get("effort", "low"), thinking=c.get("thinking", "disabled"))
    except (BudgetBlocked, KillSwitchOff, Exception):
        return {}
    out = {}
    for line in res.text.splitlines():
        try:
            r = json.loads(line.strip())
            st = cases[int(r["i"]) - 1][0]
            if r.get("new"):
                out[st["id"]] = {"fact": r.get("fact") or ""}
        except Exception:
            continue
    return out


def mock_responder(label: str, params: dict) -> str:
    user = params["messages"][0]["content"]
    n = len(re.findall(r"^CASE \d+", user, re.M))
    return "\n".join(json.dumps({"i": i, "new": 0, "fact": ""}) for i in range(1, n + 1))
