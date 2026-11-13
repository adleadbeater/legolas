"""Cost ledger: one JSON line per Claude call, priced from config/pricing.yaml.

Every call records model, input / output / cache-write / cache-read tokens, web
searches, whether it ran through the Batches API, and the dollar cost. The ledger is
also the source of truth for the daily dollar caps (see BudgetGuard).

CLI:  python -m scout.ledger summary [--ledger state/cost-ledger.jsonl] [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from scout.config import load_yaml

DEFAULT_TZ = "America/New_York"


@dataclass
class Usage:
    input_tokens: int = 0            # uncached input
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_ttl: str = "5m"            # which write price applies
    web_searches: int = 0


class Pricing:
    def __init__(self, table: Optional[dict] = None):
        self.table = table or load_yaml("config/pricing.yaml")

    def model(self, model: str) -> dict:
        try:
            return self.table["models"][model]
        except KeyError as e:
            raise KeyError(f"No price for model {model!r}; add it to config/pricing.yaml") from e

    def cost(self, model: str, u: Usage, batch: bool = False) -> float:
        p = self.model(model)
        write_rate = p["cache_write_1h"] if u.cache_ttl == "1h" else p["cache_write_5m"]
        tokens_usd = (
            u.input_tokens * p["input"]
            + u.output_tokens * p["output"]
            + u.cache_write_tokens * write_rate
            + u.cache_read_tokens * p["cache_read"]
        ) / 1_000_000
        if batch:
            tokens_usd *= self.table.get("batch_discount", 0.5)
        search_usd = u.web_searches * self.table.get("server_tools", {}).get("web_search_per_use", 0.01)
        return round(tokens_usd + search_usd, 8)

    def worst_case(self, model: str, input_tokens: int, max_tokens: int,
                   web_searches: int = 0, batch: bool = False) -> float:
        """Pessimistic pre-call estimate: all input uncached, full max_tokens used."""
        return self.cost(model, Usage(input_tokens=input_tokens, output_tokens=max_tokens,
                                      web_searches=web_searches), batch=batch)


class Ledger:
    def __init__(self, path: Path, pricing: Optional[Pricing] = None,
                 clock: Optional[Callable[[], datetime]] = None, tz: str = DEFAULT_TZ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pricing = pricing or Pricing()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = ZoneInfo(tz)
        self._spent_cache: dict = {}

    def day_of(self, ts: datetime) -> str:
        return ts.astimezone(self.tz).date().isoformat()

    def today(self) -> str:
        return self.day_of(self.clock())

    def record(self, *, app: str, lane: str, call: str, model: str, usage: Usage,
               batch: bool = False, mock: bool = False, run_id: str = "",
               extra: Optional[dict] = None) -> dict:
        now = self.clock()
        row = {
            "ts": now.isoformat(),
            "day": self.day_of(now),
            "app": app,
            "lane": lane,
            "call": call,
            "model": model,
            "batch": batch,
            "mock": mock,
            "run_id": run_id,
            **{k: v for k, v in asdict(usage).items()},
            "usd": self.pricing.cost(model, usage, batch=batch),
        }
        if extra:
            row.update(extra)
        with open(self.path, "a") as f:
            f.write(json.dumps(row) + "\n")
        for key in ((row["day"], app, lane), (row["day"], app, None)):
            if key in self._spent_cache:
                self._spent_cache[key] += row["usd"]
        return row

    def record_event(self, *, app: str, lane: str, event: str, detail: str = "") -> None:
        """Non-billed events (budget blocks, kill-switch stops) for the daily summary."""
        now = self.clock()
        with open(self.path, "a") as f:
            f.write(json.dumps({"ts": now.isoformat(), "day": self.day_of(now), "app": app,
                                "lane": lane, "event": event, "detail": detail, "usd": 0.0}) + "\n")

    def rows(self) -> Iterable[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _load_spent(self, day: str, app: str, lane: Optional[str]) -> float:
        total = 0.0
        for r in self.rows():
            if r.get("day") == day and r.get("app") == app and (lane is None or r.get("lane") == lane):
                total += r.get("usd", 0.0)
        return total

    def spent(self, app: str, lane: Optional[str] = None, day: Optional[str] = None) -> float:
        day = day or self.today()
        key = (day, app, lane)
        if key not in self._spent_cache:
            self._spent_cache[key] = self._load_spent(day, app, lane)
        return self._spent_cache[key]

    def summary(self, day: Optional[str] = None) -> dict:
        return summarize(self.rows(), day)


def summarize(rows: Iterable[dict], day: Optional[str] = None) -> dict:
    out: dict = {}
    for r in rows:
        if day and r.get("day") != day:
            continue
        d = out.setdefault(r["day"], {"usd": 0.0, "calls": 0, "apps": {}, "events": defaultdict(int)})
        if "event" in r:
            d["events"][f"{r['app']}:{r['event']}"] += 1
            continue
        d["usd"] += r["usd"]
        d["calls"] += 1
        a = d["apps"].setdefault(r["app"], {"usd": 0.0, "calls": 0, "by_call": {}})
        a["usd"] += r["usd"]
        a["calls"] += 1
        c = a["by_call"].setdefault(f"{r['call']}:{r['model']}", {
            "calls": 0, "usd": 0.0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "web_searches": 0})
        c["calls"] += 1
        c["usd"] += r["usd"]
        for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "web_searches"):
            c[k] += r.get(k, 0)
    for d in out.values():
        d["events"] = dict(d["events"])
    return out


def format_summary(summary: dict) -> str:
    lines = []
    for day in sorted(summary):
        d = summary[day]
        lines.append(f"{day}  total ${d['usd']:.4f}  ({d['calls']} calls)")
        for app, a in sorted(d["apps"].items()):
            lines.append(f"  {app:<12} ${a['usd']:.4f}  {a['calls']} calls")
            for key, c in sorted(a["by_call"].items(), key=lambda kv: -kv[1]["usd"]):
                lines.append(
                    f"    {key:<36} n={c['calls']:<5} in={c['input_tokens']:<8} out={c['output_tokens']:<7}"
                    f" cw={c['cache_write_tokens']:<7} cr={c['cache_read_tokens']:<8} ws={c['web_searches']:<3}"
                    f" ${c['usd']:.4f}")
        for ev, n in sorted(d["events"].items()):
            lines.append(f"  event {ev} x{n}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["summary"])
    ap.add_argument("--ledger", default="state/cost-ledger.jsonl")
    ap.add_argument("--day")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    led = Ledger(Path(args.ledger))
    s = led.summary(args.day)
    print(json.dumps(s, indent=2) if args.json else format_summary(s))


if __name__ == "__main__":
    main()
