"""Editor feedback from Legolas's OWN channel (#agent-legolas), not Boromir's.

The channel ID comes from env LEGOLAS_FEEDBACK_CHANNEL (private, never committed).
The bot must be invited to the channel and have channels:history / groups:history.

Signals
  thumbs up / down       -> tag boost +-1 on the story's primary tag
  reply                  -> Haiku turns it into {action, reason?}; reason is OPTIONAL
  "reject" (any reply starting with reject / x / no) -> feed row status=rejected with
                            the optional reason, if one was given
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import requests

from scout.feed import REJECT_REASONS, FeedWriter, reject
from scout.llm import LLM, BudgetBlocked, KillSwitchOff
from scout.store import Store

log = logging.getLogger("legolas.feedback")

SYSTEM = """You read one editor reply to a news pitch card from MovieWeb's news scout.
Return one line of JSON only:
{"action":"boost"|"penalize"|"reject"|"note"|"ignore","reason":null|"already_on_mw"|"wrong_topic"|"weak_or_unsourced"|"not_our_audience"|"too_late"|"duplicate_of_another_agent"|"not_a_story"|"facts_wrong"|"bad_angle","note":"<at most 15 words or empty>"}
Use reason only if the editor actually gave one; otherwise null. A reason is optional."""


def interpret(llm: LLM, reply: str, cfg: dict) -> Optional[dict]:
    c = cfg.get("calls", {}).get("feedback", {})
    try:
        res = llm.call("feedback", model=cfg.get("models", {}).get("feedback", "claude-haiku-5-5"),
                       system=SYSTEM, user=reply[:500], max_tokens=int(c.get("max_tokens", 150)),
                       effort=c.get("effort", "low"), thinking=c.get("thinking", "disabled"))
        m = re.search(r"\{.*\}", res.text, re.S)
        d = json.loads(m.group(0)) if m else None
    except (BudgetBlocked, KillSwitchOff) as e:
        log.info("feedback interpretation skipped: %s", e)
        return None
    except Exception:
        return None
    if d and d.get("reason") not in REJECT_REASONS:
        d["reason"] = None
    return d


def process(llm: LLM, store: Store, feed: FeedWriter, cfg: dict, now_iso: str) -> dict:
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("LEGOLAS_FEEDBACK_CHANNEL")
    stats = {"reactions": 0, "replies": 0, "rejects": 0}
    if not (token and channel):
        log.info("feedback: SLACK_BOT_TOKEN / LEGOLAS_FEEDBACK_CHANNEL not set - skipped")
        return stats
    h = {"Authorization": f"Bearer {token}"}
    try:
        data = requests.get("https://slack.com/api/conversations.history", headers=h, timeout=15,
                            params={"channel": channel, "limit": 50}).json()
    except Exception as e:
        log.warning("feedback read failed: %s", e)
        return stats
    if not data.get("ok"):
        log.warning("conversations.history: %s (is the bot in the channel?)", data.get("error"))
        return stats
    done = set(store.kv_get("feedback_done", []))
    ts_to_story = store.kv_get("slack_ts_to_story", {})
    boosts = store.kv_get("tag_boosts", {})
    latest = feed.latest()
    for msg in data.get("messages", []):
        ts = msg.get("ts", "")
        sid = ts_to_story.get(ts)
        st = store.get_story(sid) if sid else None
        tag = ((st or {}).get("card") or {}).get("tag", "").lower()
        for r in msg.get("reactions", []):
            k = f"{ts}:{r['name']}"
            if k in done:
                continue
            done.add(k)
            if tag and r["name"] in ("+1", "thumbsup"):
                boosts[tag] = boosts.get(tag, 0) + 1
            elif tag and r["name"] in ("-1", "thumbsdown"):
                boosts[tag] = boosts.get(tag, 0) - 1
            stats["reactions"] += 1
        if msg.get("reply_count"):
            try:
                rep = requests.get("https://slack.com/api/conversations.replies", headers=h, timeout=15,
                                   params={"channel": channel, "ts": ts}).json()
            except Exception:
                continue
            for r in rep.get("messages", [])[1:]:
                k = f"{ts}:{r.get('ts')}"
                if k in done or r.get("bot_id"):
                    continue
                done.add(k)
                d = interpret(llm, r.get("text", ""), cfg)
                stats["replies"] += 1
                if d and d.get("action") == "reject" and sid and sid in latest:
                    reject(feed, sid, d.get("reason"), d.get("note") or None, now_iso)
                    stats["rejects"] += 1
                elif d and tag and d.get("action") in ("boost", "penalize"):
                    boosts[tag] = boosts.get(tag, 0) + (1 if d["action"] == "boost" else -1)
    store.kv_set("feedback_done", sorted(done)[-5000:])
    store.kv_set("tag_boosts", boosts)
    return stats
