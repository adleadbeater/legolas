"""Outlet normalisation, credibility tiers and independent-source counting (no AI)."""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

from scout.config import load_yaml
from scout.textutil import title_tokens, jaccard


class Outlets:
    def __init__(self, cfg: Optional[dict] = None, feed_tiers: Optional[dict] = None):
        cfg = cfg if cfg is not None else load_yaml("config/outlets.yaml")
        self.aliases = {k.lower(): v for k, v in (cfg.get("aliases") or {}).items()}
        self.tier = {}
        for t, names in (cfg.get("tiers") or {}).items():
            for n in names:
                self.tier[n.lower()] = int(t)
        # feeds.yaml names we read directly: default tier 2 unless listed
        for name, t in (feed_tiers or {}).items():
            self.tier.setdefault(self.canonical(name).lower(), t)
        self.parent = {}
        for parent, names in (cfg.get("corporate_parents") or {}).items():
            for n in names:
                self.parent[n.lower()] = parent
        self.sisters = {n.lower() for n in cfg.get("sister_sites") or []}
        self.self_outlets = {n.lower() for n in cfg.get("self_outlets") or []}

    def canonical(self, name: str) -> str:
        n = (name or "").strip()
        n = re.sub(r"\s+", " ", n)
        key = n.lower()
        if key in self.aliases:
            return self.aliases[key]
        key2 = re.sub(r"^www\.|\.(com|net|org|co\.uk)$", "", key)
        if key2 in self.aliases:
            return self.aliases[key2]
        return n

    def tier_of(self, name: str) -> int:
        return self.tier.get(self.canonical(name).lower(), 3)

    def is_self(self, name: str) -> bool:
        return self.canonical(name).lower() in self.self_outlets

    def is_sister(self, name: str) -> bool:
        return self.canonical(name).lower() in self.sisters

    def independence_key(self, name: str) -> str:
        c = self.canonical(name)
        return "parent:" + self.parent[c.lower()] if c.lower() in self.parent else c

    def weighted_sources(self, items: Iterable[dict], max_tier: int = 2) -> float:
        """Independent breadth. Rules:
        - only tier <= max_tier outlets count (aggregators / IMDb / wire re-runs never do)
        - MW itself never counts
        - same-parent outlets count 0.5 each, capped at 1.0 per parent... except a
          single parent outlet still counts 0.5 (matches old Legolas behaviour)
        - syndicated copy (near-identical headline at different outlets) counts once
        """
        seen_outlets = {}
        fingerprints: List[set] = []
        for it in items:
            name = self.canonical(it.get("source_name", ""))
            if self.is_self(name) or self.tier_of(name) > max_tier:
                continue
            toks = title_tokens(it.get("title", ""))
            if any(jaccard(toks, f) >= 0.8 for f in fingerprints) and name not in seen_outlets:
                # wire / syndication copy of a headline we already counted
                continue
            fingerprints.append(toks)
            seen_outlets[name] = True
        weight = 0.0
        per_parent: dict = {}
        for name in seen_outlets:
            key = self.independence_key(name)
            if key.startswith("parent:"):
                per_parent[key] = per_parent.get(key, 0) + 0.5
            else:
                weight += 1.0
        return weight + sum(per_parent.values())

    def has_credible_second_source(self, items: Iterable[dict]) -> bool:
        keys = {self.independence_key(it["source_name"]) for it in items
                if self.tier_of(it["source_name"]) <= 2 and not self.is_self(it["source_name"])}
        return len(keys) >= 2
