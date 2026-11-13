"""Config loading helpers. Secrets are read from the environment only."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    with open(p) as f:
        return yaml.safe_load(f) or {}


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def state_dir(cfg: dict) -> Path:
    """State lives outside git: env SCOUT_STATE_DIR wins, else config state_dir."""
    d = os.environ.get("SCOUT_STATE_DIR") or cfg.get("state_dir", "state")
    p = Path(d)
    if not p.is_absolute():
        p = REPO_ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default
