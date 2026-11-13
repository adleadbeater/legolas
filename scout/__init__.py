"""Shared scout library for Legolas, the Little Elves and Celebrimbor.

Everything that is not specific to one agent lives here: feed fetching, outlet
normalisation and credibility tiers, the story store, entity extraction, entity+event
clustering, momentum, the MW coverage check, the JSON-line story feed, and the cost
controls (ledger, daily caps, kill switch, model client wrapper).

Cost rule: nothing in collection, matching, dedupe or polling calls a model. Model
calls go through scout.llm.LLM, which checks the kill switch and the daily cap first
and writes every call to the cost ledger.
"""

__all__ = ["config", "outlets", "textutil", "filters", "fetch", "store", "extract",
           "cluster", "momentum", "mwcheck", "llm", "ledger", "killswitch", "feed",
           "websearch", "tags"]
