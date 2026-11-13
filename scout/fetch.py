"""Collection (no AI): RSS / Atom feeds from config/feeds.yaml, and MW's own news
sitemap for the coverage check. Google News search was removed on 13 Nov 2026.

RSS and official feeds only - no page scraping. The text kept per article is the feed
summary plus the first paragraph of content:encoded when the feed carries it.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import feedparser
import requests

from scout.outlets import Outlets
from scout.textutil import article_text

log = logging.getLogger("scout.fetch")
UA = {"User-Agent": "Legolas/2.0 (+RSS reader)"}


def _entry_dt(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        v = getattr(entry, attr, None)
        if v:
            return datetime(*v[:6], tzinfo=timezone.utc)
    return None


def parse_feed(content: bytes, source_name: str, source_tier: int, outlets: Outlets,
               cutoff: datetime, max_text_chars: int, via: str = "rss") -> List[dict]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        title = (getattr(e, "title", "") or "").strip()
        if not title:
            continue
        dt = _entry_dt(e)
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt < cutoff:
            continue
        name = outlets.canonical(source_name)
        content_html = ""
        if e.get("content"):
            content_html = e["content"][0].get("value", "")
        out.append({
            "guid": getattr(e, "id", None) or getattr(e, "link", None) or title,
            "title": title,
            "url": getattr(e, "link", "") or "",
            "published_dt": dt,
            "source_name": name,
            "source_tier": source_tier,
            "text": article_text(getattr(e, "summary", "") or "", content_html, max_text_chars),
            "via": via,
        })
    return out


def fetch_feeds(sources: Dict[str, str], tiers: Dict[str, int], outlets: Outlets,
                lookback_minutes: int, max_text_chars: int = 600, timeout: int = 15,
                workers: int = 12) -> List[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    def one(kv):
        name, url = kv
        try:
            r = requests.get(url, timeout=timeout, headers=UA)
            r.raise_for_status()
            return parse_feed(r.content, outlets.canonical(name), tiers.get(name, 2), outlets,
                              cutoff, max_text_chars)
        except Exception as e:  # one broken feed never stops a run
            log.warning("feed %s failed: %s", name, e)
            return []

    with ThreadPoolExecutor(workers) as ex:
        return [i for chunk in ex.map(one, sources.items()) for i in chunk]


_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
       "news": "http://www.google.com/schemas/sitemap-news/0.9"}


def parse_news_sitemap(xml_bytes: bytes) -> List[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for u in root.findall("s:url", _NS):
        loc = u.findtext("s:loc", default="", namespaces=_NS)
        title = u.findtext("news:news/news:title", default="", namespaces=_NS)
        pub = u.findtext("news:news/news:publication_date", default="", namespaces=_NS)
        if not (loc and title and pub):
            continue
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append({"url": loc, "title": title, "published_dt": dt})
    return out


def fetch_mw_articles(sitemap_url: str, timeout: int = 15) -> List[dict]:
    try:
        r = requests.get(sitemap_url, timeout=timeout, headers=UA)
        r.raise_for_status()
        return parse_news_sitemap(r.content)
    except Exception as e:
        log.warning("MW sitemap failed: %s", e)
        return []


def slug_words(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return re.sub(r"[-_/]+", " ", path).strip()
