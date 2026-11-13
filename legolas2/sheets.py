"""MW tag sheet + perennials / new releases (read-only), same sheets as V1.
Optional: without Google credentials the run continues with no tags (no Proven Topic)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from scout.tags import is_junk_tag

log = logging.getLogger("legolas.sheets")


def _svc():
    import google.auth
    import google.auth.transport.requests
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(google.auth.transport.requests.Request())
    return build("sheets", "v4", credentials=creds)


def _parse_date(s: str):
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def load(v1cfg: dict, tag_sa_min: int, tag_fr_max: float, stopwords) -> Tuple[Dict, Dict]:
    try:
        svc = _svc()
    except Exception as e:
        log.warning("Google Sheets unavailable (%s) - running without MW tags / watched topics", e)
        return {}, {}
    tags, watched = {}, {}
    sh = v1cfg["sheets"]
    try:
        tabs = {s["properties"]["title"] for s in svc.spreadsheets().get(spreadsheetId=sh["sheet_id"]).execute()["sheets"]}
        tab = next((t for t in sh["tag_tab_names"] if t in tabs), None)
        rows = svc.spreadsheets().values().get(spreadsheetId=sh["sheet_id"], range=f"{tab}!A:Z").execute().get("values", []) if tab else []
        hdr = [h.strip().lower() for h in rows[0]] if rows else []
        col = lambda *names: next((i for i, h in enumerate(hdr) for n in names if n in h), None)
        tc, sc, fc = col("tag"), col("s/a", "sa"), col("fr", "fail")
        for r in rows[1:]:
            try:
                tag = r[tc].strip().lower()
                sa = float(str(r[sc]).replace(",", "")) if sc is not None and sc < len(r) else 0
                fr = float(str(r[fc]).replace("%", "")) if fc is not None and fc < len(r) else 0
            except (IndexError, ValueError, TypeError):
                continue
            if tag and not is_junk_tag(tag, stopwords) and sa >= tag_sa_min and fr < tag_fr_max:
                tags[tag] = {"sa": sa, "fr": fr}
    except Exception as e:
        log.warning("tag sheet read failed: %s", e)
    p = v1cfg.get("perennials", {})
    today = datetime.now(timezone.utc).date()
    try:
        start = p.get("data_start_row", 7)
        for r in svc.spreadsheets().values().get(spreadsheetId=p["sheet_id"], range=f"'{p['perennials_tab']}'!A{start}:C").execute().get("values", []):
            if r and r[0].strip():
                watched[r[0].strip().lower()] = {"name": r[0].strip(), "type": r[1] if len(r) > 1 else "Other",
                                                 "category": "perennial", "boost": p.get("boost_perennial", 1)}
        for r in svc.spreadsheets().values().get(spreadsheetId=p["sheet_id"], range=f"'{p['new_release_tab']}'!A{start}:C").execute().get("values", []):
            d = _parse_date(r[2]) if len(r) > 2 else None
            if not d or not (d - timedelta(days=p.get("release_window_before", 14)) <= today <= d + timedelta(days=p.get("release_window_after", 42))):
                continue
            hot = -p.get("hot_window_before", 5) <= (today - d).days <= p.get("hot_window_after", 10)
            watched[r[0].strip().lower()] = {"name": r[0].strip(), "type": r[1] if len(r) > 1 else "Other",
                                             "category": "new_release", "release_date": d.isoformat(), "_hot": hot,
                                             "boost": p.get("boost_new_release_hot", 2) if hot else p.get("boost_new_release", 1)}
    except Exception as e:
        log.warning("perennials read failed: %s", e)
    return tags, watched
