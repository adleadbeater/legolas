"""The only way scout agents call Claude.

Every call:
  1. checks the kill switch (app + lane),
  2. checks the app's daily dollar cap with a pessimistic estimate (all input uncached,
     full max_tokens) and refuses if it would cross it,
  3. sends static system text as a cached block (TTL from config; 1h by default
     because runs are 15 min apart),
  4. records model, input / output / cache-write / cache-read tokens, web searches and
     dollars to the cost ledger.

Backends
  AnthropicBackend   real API via the official SDK (ANTHROPIC_API_KEY from env only)
  MockBackend        no network; deterministic responders + simulated prompt caching.
                     Token counts are estimates from text length.
  RecordedBackend    replays recorded real responses; falls back to a mock responder
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

from scout.killswitch import KillSwitch
from scout.ledger import Ledger, Usage
from scout.textutil import est_tokens

log = logging.getLogger("scout.llm")

MIN_CACHEABLE = 512  # Haiku 5.5 / Sonnet 5.5 minimum cacheable prefix


class BudgetBlocked(RuntimeError):
    pass


class KillSwitchOff(RuntimeError):
    pass


@dataclass
class Result:
    text: str
    usage: Usage
    usd: float
    stop_reason: str = "end_turn"


def build_params(model: str, system: str, user: str, max_tokens: int, effort: Optional[str],
                 thinking: Optional[str], cache_ttl: str, tools: Optional[list] = None) -> dict:
    cc = {"type": "ephemeral"}
    if cache_ttl == "1h":
        cc["ttl"] = "1h"
    p = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": cc}],
        "messages": [{"role": "user", "content": user}],
    }
    if effort:
        p["output_config"] = {"effort": effort}
    if thinking:
        p["thinking"] = {"type": thinking}
    if tools:
        p["tools"] = tools
    return p


def _usage_from_sdk(u, ttl: str) -> Usage:
    stu = getattr(u, "server_tool_use", None)
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_ttl=ttl,
        web_searches=(getattr(stu, "web_search_requests", 0) or 0) if stu else 0,
    )


class AnthropicBackend:
    mock = False

    def __init__(self, timeout: float = 60.0):
        import anthropic  # imported lazily so tests/replay need no key
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=2)

    def create(self, label: str, params: dict, ttl: str, now: datetime):
        a = self.anthropic
        try:
            msg = self.client.messages.create(**params)
        except a.BadRequestError as e:
            log.error("Claude 400 on %s: %s", label, e.message)
            raise
        except a.RateLimitError:
            log.warning("Claude rate-limited on %s", label)
            raise
        except a.APIStatusError as e:
            log.warning("Claude %s on %s", e.status_code, label)
            raise
        except a.APIConnectionError:
            log.warning("Claude connection error on %s", label)
            raise
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return text, _usage_from_sdk(msg.usage, ttl), msg.stop_reason

    # Message Batches (50% off) for jobs where latency doesn't matter.
    def submit_batch(self, reqs: List[dict]) -> str:
        b = self.client.messages.batches.create(
            requests=[{"custom_id": r["custom_id"], "params": r["params"]} for r in reqs])
        return b.id

    def collect_batch(self, batch_id: str, ttl: str):
        b = self.client.messages.batches.retrieve(batch_id)
        if b.processing_status != "ended":
            return None
        out = {}
        for r in self.client.messages.batches.results(batch_id):
            if r.result.type == "succeeded":
                m = r.result.message
                out[r.custom_id] = ("".join(x.text for x in m.content if getattr(x, "type", "") == "text"),
                                    _usage_from_sdk(m.usage, ttl))
        return out


Responder = Callable[[str, dict], str]


class MockBackend:
    """No network. A responder per call label produces realistic output; usage is
    estimated from text length and prompt caching is simulated per (model, system)."""
    mock = True

    def __init__(self, responders: Dict[str, Responder], searches_per_call: int = 0):
        self.responders = responders
        self.cache: Dict[tuple, datetime] = {}
        self.searches_per_call = searches_per_call
        self._batches: Dict[str, list] = {}

    def _usage(self, params: dict, text: str, ttl: str, now: datetime) -> Usage:
        sys_text = "".join(b["text"] for b in params.get("system", []))
        user = params["messages"][0]["content"]
        sys_tok, user_tok = est_tokens(sys_text), est_tokens(user)
        key = (params["model"], hashlib.sha1(sys_text.encode()).hexdigest())
        life = timedelta(hours=1) if ttl == "1h" else timedelta(minutes=5)
        u = Usage(output_tokens=min(est_tokens(text), params["max_tokens"]), cache_ttl=ttl)
        if sys_tok < MIN_CACHEABLE:
            u.input_tokens = sys_tok + user_tok
        elif key in self.cache and now - self.cache[key] < life:
            u.cache_read_tokens = sys_tok
            u.input_tokens = user_tok
            self.cache[key] = now  # reads refresh the TTL
        else:
            u.cache_write_tokens = sys_tok
            u.input_tokens = user_tok
            self.cache[key] = now
        if params.get("tools"):
            u.web_searches = self.searches_per_call
            u.input_tokens += 2500 * u.web_searches  # search results come back as input
        return u

    def create(self, label: str, params: dict, ttl: str, now: datetime):
        text = self.responders[label](label, params)
        return text, self._usage(params, text, ttl, now), "end_turn"

    def submit_batch(self, reqs: List[dict]) -> str:
        bid = "mockbatch-" + hashlib.sha1(json.dumps([r["custom_id"] for r in reqs]).encode()).hexdigest()[:8]
        self._batches[bid] = reqs
        return bid

    def collect_batch(self, batch_id: str, ttl: str, now: Optional[datetime] = None):
        reqs = self._batches.pop(batch_id, [])
        out = {}
        for r in reqs:
            text = self.responders[r["label"]](r["label"], r["params"])
            out[r["custom_id"]] = (text, self._usage(r["params"], text, ttl, now or datetime.now()))
        return out


class RecordedBackend(MockBackend):
    """Replays real responses recorded by RecordingBackend (keyed by request hash)."""

    def __init__(self, path: Path, responders: Dict[str, Responder]):
        super().__init__(responders)
        self.rec = {}
        if Path(path).exists():
            for line in open(path):
                r = json.loads(line)
                self.rec[r["key"]] = r

    def create(self, label, params, ttl, now):
        k = request_key(params)
        if k in self.rec:
            r = self.rec[k]
            return r["text"], Usage(**r["usage"]), r.get("stop_reason", "end_turn")
        return super().create(label, params, ttl, now)


def request_key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


class LLM:
    def __init__(self, app: str, backend, ledger: Ledger, killswitch: KillSwitch,
                 daily_cap_usd: float, cache_ttl: str = "1h", run_id: str = "",
                 record_path: Optional[Path] = None):
        self.app = app
        self.backend = backend
        self.ledger = ledger
        self.ks = killswitch
        self.cap = daily_cap_usd
        self.ttl = cache_ttl
        self.run_id = run_id
        self.record_path = record_path
        self.budget_hit = False   # set once the daily cap blocks a call in this run

    def remaining(self) -> float:
        return self.cap - self.ledger.spent(self.app)

    def _guard(self, lane: str, model: str, est_in: int, max_tokens: int, searches: int,
               batch: bool) -> None:
        why = self.ks.reason_off(lane)
        if why:
            self.ledger.record_event(app=self.app, lane=lane, event="killswitch", detail=why)
            raise KillSwitchOff(why)
        worst = self.ledger.pricing.worst_case(model, est_in, max_tokens, searches, batch)
        if self.ledger.spent(self.app) + worst > self.cap:
            self.budget_hit = True
            self.ledger.record_event(app=self.app, lane=lane, event="budget_block",
                                     detail=f"spent={self.ledger.spent(self.app):.4f} est={worst:.4f} cap={self.cap}")
            log.warning("%s daily cap $%.2f reached (spent $%.4f, next call est. $%.4f) - call blocked",
                        self.app, self.cap, self.ledger.spent(self.app), worst)
            raise BudgetBlocked(f"{self.app} daily cap ${self.cap:.2f} reached")

    def call(self, label: str, *, model: str, system: str, user: str, max_tokens: int,
             effort: Optional[str] = "low", thinking: Optional[str] = None,
             lane: Optional[str] = None, tools: Optional[list] = None,
             max_searches: int = 0) -> Result:
        lane = lane or self.app
        params = build_params(model, system, user, max_tokens, effort, thinking, self.ttl, tools)
        self._guard(lane, model, est_tokens(system) + est_tokens(user) + 2500 * max_searches,
                    max_tokens, max_searches, False)
        text, usage, stop = self.backend.create(label, params, self.ttl, self.ledger.clock())
        row = self.ledger.record(app=self.app, lane=lane, call=label, model=model, usage=usage,
                                 mock=self.backend.mock, run_id=self.run_id,
                                 extra={"stop_reason": stop})
        if self.record_path and not self.backend.mock:
            with open(self.record_path, "a") as f:
                f.write(json.dumps({"key": request_key(params), "label": label, "text": text,
                                    "usage": usage.__dict__, "stop_reason": stop}) + "\n")
        if stop == "max_tokens":
            log.warning("%s hit max_tokens=%s - output may be truncated", label, max_tokens)
        return Result(text=text, usage=usage, usd=row["usd"], stop_reason=stop)

    # ── Batches (non-urgent jobs, 50% off) ───────────────────────────────────
    def submit_batch(self, label: str, *, model: str, system: str, users: Dict[str, str],
                     max_tokens: int, effort: Optional[str] = "low",
                     thinking: Optional[str] = None, lane: Optional[str] = None) -> str:
        lane = lane or self.app
        est = sum(est_tokens(system) + est_tokens(u) for u in users.values())
        self._guard(lane, model, est, max_tokens * len(users), 0, True)
        reqs = [{"custom_id": cid, "label": label,
                 "params": build_params(model, system, u, max_tokens, effort, thinking, self.ttl)}
                for cid, u in users.items()]
        return self.backend.submit_batch(reqs)

    def collect_batch(self, batch_id: str, label: str, model: str,
                      lane: Optional[str] = None) -> Optional[Dict[str, str]]:
        lane = lane or self.app
        if self.backend.mock:
            res = self.backend.collect_batch(batch_id, self.ttl, self.ledger.clock())
        else:
            res = self.backend.collect_batch(batch_id, self.ttl)
        if res is None:
            return None
        out = {}
        for cid, (text, usage) in res.items():
            self.ledger.record(app=self.app, lane=lane, call=label, model=model, usage=usage,
                               batch=True, mock=self.backend.mock, run_id=self.run_id)
            out[cid] = text
        return out
