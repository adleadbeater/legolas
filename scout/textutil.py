"""Text helpers: HTML stripping, tokens, Jaccard, whole-phrase matching, entity keys."""
from __future__ import annotations

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

TITLE_SIM_STOP = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by",
    "from", "as", "is", "was", "are", "be", "been", "its", "it", "this", "that", "these",
    "those", "his", "her", "their", "our", "your", "who", "what", "when", "how", "new",
    "says", "said", "told", "reveals", "reveal", "revealed", "confirms", "confirm",
    "confirmed", "announces", "announce", "announced", "gets", "get", "got", "sets",
    "set", "joins", "join", "joined", "adds", "add", "drops", "drop", "returns", "return",
    "shares", "share", "talks", "teases", "opens", "makes", "gives", "calls", "reacts",
    "season", "seasons", "series", "episode", "episodes", "released", "release",
    "window", "report", "reports", "first", "look", "watch", "details", "detail",
    "update", "updates", "news", "story", "stories", "here", "more", "into", "way",
}


def strip_html(s: str) -> str:
    s = html.unescape(_TAG_RE.sub(" ", s or ""))
    return _WS_RE.sub(" ", s).strip()


def first_paragraph(content_html: str, max_chars: int = 500) -> str:
    if not content_html:
        return ""
    parts = re.split(r"</p>|<br\s*/?>\s*<br\s*/?>", content_html, flags=re.I)
    for p in parts:
        t = strip_html(p)
        if len(t) >= 40:
            return t[:max_chars]
    return strip_html(content_html)[:max_chars]


def article_text(summary: str, content_html: str, max_chars: int) -> str:
    """Summary + first paragraph, de-duplicated, capped. Never the full article."""
    s = strip_html(summary)
    fp = first_paragraph(content_html, max_chars)
    if fp and fp[:60].lower() not in s.lower():
        s = (s + " " + fp).strip() if s else fp
    return s[:max_chars]


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return s.lower()


def entity_key(name: str) -> str:
    s = fold(name)
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def title_tokens(text: str) -> set:
    t = re.sub(r"[^\w\s]", " ", fold(text))
    return {w for w in t.split() if len(w) > 3 and w not in TITLE_SIM_STOP}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def titles_match(a: str, b: str, threshold: float = 0.5) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    return len(ta & tb) >= 3 and jaccard(ta, tb) >= threshold


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word / whole-phrase, case-insensitive. 'From' never matches 'from' inside
    a sentence unless the caller passes case_sensitive phrases (see tags.py)."""
    if not phrase:
        return False
    pat = r"(?<![a-z0-9])" + re.escape(fold(phrase)) + r"(?![a-z0-9])"
    return re.search(pat, fold(text)) is not None


def est_tokens(text: str) -> int:
    """Rough token estimate for budgeting / mock usage (newer tokenizers ~3.3 chars/token)."""
    return max(1, int(len(text or "") / 3.3) + 1)
