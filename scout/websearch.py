"""Gated web-search verification. Only for a story that ALREADY passed matching and
gates but lacks a second credible independent source (or has a fact flagged to
check). max_uses 1-2 per call, hard per-lane daily cap, every search in the ledger."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from scout.llm import LLM, BudgetBlocked, KillSwitchOff
from scout.store import Store

log = logging.getLogger("scout.websearch")

SYSTEM = """You verify one entertainment news claim for MovieWeb's news desk using web search.
Search once. Decide whether a credible outlet OTHER than the original source independently reports the same fact (trades, national press, studio or network statements count; aggregators, rumour accounts and re-posts of the same article do not).
Reply with one line of JSON only:
{"confirmed":0|1,"source":"<outlet that confirms, or empty>","url":"<url or empty>","note":"<at most 20 words: what is confirmed, or what is still unverified>"}
Never add facts that the search results do not state."""


def verify(llm: LLM, store: Store, story: dict, cfg: dict, day: str, lane: str) -> Optional[dict]:
    ws = cfg.get("web_search", {})
    if not ws.get("enabled"):
        return None
    key = f"websearch:{lane}"
    if store.count(day, key) >= int(ws.get("daily_cap", 3)):
        return None
    max_uses = max(1, min(2, int(ws.get("max_uses_per_call", 1))))
    tool = {"type": ws.get("tool_type", "web_search_20250305"), "name": "web_search",
            "max_uses": max_uses}
    user = (f"Claim: {story['key_fact']}\nOriginal source: {story.get('key_fact_source', '')}\n"
            f"Names: {', '.join(e['name'] for e in story['entities'][:4])}")
    try:
        res = llm.call("verify", model=cfg.get("models", {}).get("verify", "claude-haiku-5-5"),
                       system=SYSTEM, user=user, max_tokens=150, effort="low",
                       thinking=None, lane=lane, tools=[tool], max_searches=max_uses)
    except (BudgetBlocked, KillSwitchOff) as e:
        log.info("verify skipped: %s", e)
        return None
    except Exception as e:
        log.warning("verify failed: %s", e)
        return None
    store.incr(day, key, max(1, res.usage.web_searches))
    m = re.search(r"\{.*\}", res.text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def mock_responder(label: str, params: dict) -> str:
    return json.dumps({"confirmed": 1, "source": "Variety", "url": "",
                       "note": "Mock verification - no search performed."})
