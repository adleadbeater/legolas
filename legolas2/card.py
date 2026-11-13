"""Card writing: Sonnet 5.5 for the top N candidates per run, one batched call.

The "What happened" line is protected: it starts from the extracted key fact, the
model may only tighten it, and if the model's version drops any name or number from
the key fact (or is empty) the original fact line is used. The title step never
touches it. This is the fix for cards that lost the actor's name or the date.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

from scout.config import REPO_ROOT
from scout.llm import LLM, BudgetBlocked, KillSwitchOff
from scout.private_text import load_title_rules
from scout.textutil import fold

log = logging.getLogger("legolas.card")


def title_rules(cfg: dict) -> str:
    """Full rules from the LEGOLAS_TITLE_RULES secret (or _B64 / _PATH); else the public
    placeholder. See scout/private_text.py."""
    tr = cfg.get("title_rules", {})
    return load_title_rules(tr.get("fallback", "config/prompts/title_rules_public.md"))


def system_prompt(cfg: dict) -> str:
    return (REPO_ROOT / "config/prompts/card_system.md").read_text() + "\n" + title_rules(cfg)


def story_block(n: int, c: dict) -> str:
    st = c["story"]
    facts = [f for f in st["facts"]][-6:]
    lines = [f"STORY {n}{' (UPDATE)' if c['kind'] == 'update' else ''}",
             f"KEY FACT: {c['key_fact']}",
             f"EVENT: {st['event_type']}  |  REPORT/RUMOUR: {'yes' if st.get('rumour') else 'no'}",
             f"ENTITIES: {', '.join(e['name'] for e in st['entities'][:6])}",
             f"MW STATUS: {c['mw']['label']}" + (f" - MW ran \"{c['mw']['mw_title']}\" at {c['mw']['mw_at'][:16]}" if c['mw'].get('mw_title') else ""),
             f"SOURCES: {c['sources_line']}"]
    if c["kind"] == "update" and st.get("card"):
        lines.append(f"ALREADY POSTED: {st['card'].get('what_happened', '')}")
    lines.append("ITEMS:")
    for f in facts:
        lines.append(f"- {f['source']} (T{f['tier']}): {f['fact']}")
    if c.get("verify"):
        lines.append(f"VERIFICATION: {c['verify'].get('note', '')}")
    return "\n".join(lines)


_NUM = re.compile(r"\$?\d[\d,\.]*\s?(?:m|million|b|billion|%)?", re.I)


def protected_fact(original: str, proposed: str, names: List[str]) -> str:
    """Keep the model's tightened line only if it still carries every name and number."""
    p = (proposed or "").strip()
    if not p:
        return original
    fp, fo = fold(p), fold(original)
    for n in names:
        if fold(n) in fo and fold(n) not in fp:
            return original
    for num in _NUM.findall(original):
        num = num.strip()
        if len(num) >= 2 and fold(num) not in fp:
            return original
    return p


def enforce_title(title: str, rumour: bool, fallback: str) -> str:
    t = (title or "").strip().strip('"')
    if not t:
        t = fallback
    if rumour:
        t = re.sub(r"\bOfficially\s+", "", t)
    return t[:95].rsplit(" ", 1)[0] if len(t) > 95 else t


def parse(text: str, n: int) -> Dict[int, dict]:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
            out[int(r["i"])] = r
        except Exception:
            continue
    return {k: v for k, v in out.items() if 1 <= k <= n}


def python_card(c: dict) -> dict:
    """No-AI card (budget exhausted / kill switch on the card model)."""
    st = c["story"]
    return {"keep": 1, "title": st["facts"][0]["fact"][:95] if st["facts"] else c["key_fact"][:95],
            "what_happened": c["key_fact"], "angles": [], "tag": st.get("primary_entity", ""),
            "kill": "", "no_ai": True}


def write_cards(llm: LLM, cands: List[dict], cfg: dict) -> List[dict]:
    if not cands:
        return []
    calls = cfg.get("calls", {}).get("card", {})
    model = cfg.get("models", {}).get("card", "claude-sonnet-5-5")
    user = "\n\n".join(story_block(i, c) for i, c in enumerate(cands, 1))
    parsed: Dict[int, dict] = {}
    try:
        res = llm.call("card", model=model, system=system_prompt(cfg), user=user,
                       max_tokens=int(calls.get("max_tokens_per_card", 260)) * len(cands) + 40,
                       effort=calls.get("effort", "low"),
                       thinking=calls.get("thinking", "between_tools"))
        parsed = parse(res.text, len(cands))
    except (BudgetBlocked, KillSwitchOff) as e:
        log.warning("cards: %s", e)
        if cfg.get("budget", {}).get("on_exhausted", "headline_only") == "skip":
            return []
    except Exception as e:
        log.warning("card call failed: %s", e)
    out = []
    for i, c in enumerate(cands, 1):
        r = parsed.get(i) or python_card(c)
        st = c["story"]
        names = [e["name"] for e in st["entities"] if e["type"] in ("ti", "pe")]
        card = {
            "keep": bool(r.get("keep", 1)),
            "kill": r.get("kill", ""),
            "title": enforce_title(r.get("title", ""), bool(st.get("rumour")), c["key_fact"]),
            "what_happened": protected_fact(c["key_fact"], r.get("what_happened", ""), names),
            "angles": [a for a in (r.get("angles") or []) if isinstance(a, str)][:2],
            "tag": r.get("tag") or st.get("primary_entity", ""),
            "no_ai": bool(r.get("no_ai")),
        }
        out.append({**c, "card": card})
    return out


def mock_responder(label: str, params: dict) -> str:
    """Mock Sonnet card output with realistic lengths."""
    user = params["messages"][0]["content"]
    out = []
    for block in user.split("\n\n"):
        m = re.match(r"STORY (\d+)", block)
        kf = re.search(r"KEY FACT: (.*)", block)
        if not m or not kf:
            continue
        fact = kf.group(1)
        out.append(json.dumps({
            "i": int(m.group(1)), "keep": 1,
            "title": fact[:92],
            "what_happened": fact,
            "angles": ["Check: what this means for the next release window and cast.",
                       "Follow-up on the creator's earlier comments about the project."],
            "tag": "", "kill": ""}))
    return "\n".join(out)
