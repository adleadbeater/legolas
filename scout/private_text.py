"""Private prompt text that must never be committed to this PUBLIC repo.

The full MovieWeb title rules contain MW performance data, so they are supplied at run
time from the environment (a GitHub Actions secret in CI), checked in this order:

  LEGOLAS_TITLE_RULES       the rules text itself
  LEGOLAS_TITLE_RULES_B64   the same text, base64-encoded (safer to paste: one line)
  LEGOLAS_TITLE_RULES_PATH  a local file path (for running on a laptop)

If none is set, the public placeholder (config/prompts/title_rules_public.md, the
qualitative rules only) is used and a warning is logged. The rules text itself is
never logged; only which source was used and its length.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from scout.config import REPO_ROOT

log = logging.getLogger("scout.private_text")

PUBLIC_FALLBACK = "config/prompts/title_rules_public.md"
ENV_TEXT = "LEGOLAS_TITLE_RULES"
ENV_B64 = "LEGOLAS_TITLE_RULES_B64"
ENV_PATH = "LEGOLAS_TITLE_RULES_PATH"
MAX_CHARS = 60_000  # GitHub secrets max out at 48 KB; anything bigger is a mistake


def _from_env() -> Tuple[Optional[str], str]:
    raw = os.environ.get(ENV_TEXT, "")
    if raw.strip():
        return raw, ENV_TEXT
    b64 = os.environ.get(ENV_B64, "")
    if b64.strip():
        try:
            return base64.b64decode("".join(b64.split()), validate=True).decode("utf-8"), ENV_B64
        except (binascii.Error, UnicodeDecodeError, ValueError):
            log.error("%s is set but is not valid base64 UTF-8 text - ignoring it", ENV_B64)
    p = os.environ.get(ENV_PATH, "")
    if p:
        path = Path(p).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8"), ENV_PATH
        log.error("%s points at a file that does not exist - ignoring it", ENV_PATH)
    return None, ""


def load_title_rules(fallback: str = PUBLIC_FALLBACK) -> str:
    text, src = _from_env()
    if text is not None and text.strip():
        if len(text) > MAX_CHARS:
            log.warning("title rules from %s are %d chars; truncating to %d", src, len(text), MAX_CHARS)
            text = text[:MAX_CHARS]
        log.info("title rules: private rules loaded from %s (%d chars)", src, len(text))
        return text
    log.warning("title rules: no private rules set (%s / %s / %s); using the public placeholder",
                ENV_TEXT, ENV_B64, ENV_PATH)
    fb = Path(fallback)
    if not fb.is_absolute():
        fb = REPO_ROOT / fb
    return fb.read_text(encoding="utf-8")
