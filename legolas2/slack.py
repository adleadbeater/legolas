"""Lean Slack cards. Four blocks: label line, title, What happened, one meta line + link.
Angles, related stories and the article list live in the story feed, not in Slack.

Posting is OFF unless config slack.post_enabled is true AND credentials are in env.
Updates go in the original card's thread when the bot token and the card's ts are
known; otherwise a compact "Update" card is posted.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("legolas.slack")
ET = ZoneInfo("America/New_York")
TIER_EMOJI = {"trending": "📈", "proven_topic": "🎯", "legolas_special": "⭐"}


def blocks(c: dict, now: datetime) -> list:
    card, st, mw = c["card"], c["story"], c["mw"]
    label = "UPDATE" if c["kind"] == "update" else {"trending": "Big Trend", "proven_topic": "Proven Topic",
                                                   "legolas_special": "Special Pick"}[c["tier"]]
    top = f"{TIER_EMOJI.get(c['tier'], '')} *{label}* · {st['event_type'].replace('_', ' ')} · {now.astimezone(ET):%-I:%M%p} ET"
    if c.get("momentum", {}).get("label") in ("rising", "high"):
        top += f" · {c['momentum']['label']}"
    meta = f"MW: {mw['label']}"
    if mw.get("mw_at") and mw["status"] != "not_on_mw":
        meta += f" ({datetime.fromisoformat(mw['mw_at']).astimezone(ET):%-I:%M%p})"
    meta += f" · {c['sources_line']}"
    url = c.get("url") or ""
    out = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": top}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{card['title']}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What happened:* {card['what_happened']}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": meta + (f" · <{url}|source>" if url else "")}]},
    ]
    return out


def post(c: dict, cfg: dict, now: datetime, thread_ts: Optional[str] = None) -> Optional[str]:
    """Returns the message ts when known, '' when posted by webhook, None when not posted."""
    if not cfg.get("slack", {}).get("post_enabled"):
        return None
    payload = {"text": f"{c['card']['title']}", "blocks": blocks(c, now)}
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("LEGOLAS_POST_CHANNEL")
    try:
        if token and channel:
            body = {"channel": channel, **payload}
            if thread_ts:
                body["thread_ts"] = thread_ts
            r = requests.post("https://slack.com/api/chat.postMessage", timeout=15,
                              headers={"Authorization": f"Bearer {token}"}, json=body).json()
            if r.get("ok"):
                return r.get("ts")
            log.warning("chat.postMessage failed: %s", r.get("error"))
            return None
        hook = os.environ.get("SLACK_WEBHOOK")
        if hook:
            requests.post(hook, json=payload, timeout=15).raise_for_status()
            return ""
    except Exception as e:
        log.warning("Slack post failed: %s", e)
    return None
