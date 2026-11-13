"""Momentum (no AI): independent credible outlets per hour since the story's first
report, measured over the first `window_minutes`. Lets a story surface while it is
rising instead of waiting for four outlets."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from scout.outlets import Outlets


def momentum(items: List[dict], outlets: Outlets, window_minutes: int = 120) -> dict:
    if not items:
        return {"per_hour": 0.0, "sources_in_window": 0.0, "label": "none"}
    first = min(i["published_dt"] for i in items)
    win = [i for i in items if i["published_dt"] <= first + timedelta(minutes=window_minutes)]
    w = outlets.weighted_sources(win)
    span_h = max((max(i["published_dt"] for i in win) - first).total_seconds() / 3600, 0.5)
    per_hour = round(w / span_h, 2)
    label = "high" if w >= 4 else "rising" if w >= 2 else "low"
    return {"per_hour": per_hour, "sources_in_window": w, "label": label}
