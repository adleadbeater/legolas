"""Kill switch: one global toggle, one per app, one per lane (topic / watch item).

Checked at the start of every run AND before every model call, so flipping it stops
spending within one call. Sources, any of which can switch an agent off:
  * env SCOUT_KILL=1 (everything) or <APP>_KILL=1 (e.g. LEGOLAS_KILL=1, ELVES_KILL=1)
  * state/killswitch.json, e.g.
      {"agents_on": true,
       "apps":  {"legolas": true, "elves": true, "celebrimbor": true},
       "lanes": {"elves:horror": false}}
    (the News settings page can write this file; missing keys mean "on")
  * the app config's `enabled: false`
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


class KillSwitch:
    def __init__(self, path: Path, app: str, config_enabled: bool = True):
        self.path = Path(path)
        self.app = app
        self.config_enabled = config_enabled

    def _file(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except Exception:
            # A corrupt switch file fails safe: treat as OFF.
            return {"agents_on": False}

    def reason_off(self, lane: Optional[str] = None) -> Optional[str]:
        if os.environ.get("SCOUT_KILL") == "1":
            return "env SCOUT_KILL=1"
        if os.environ.get(f"{self.app.upper()}_KILL") == "1":
            return f"env {self.app.upper()}_KILL=1"
        if not self.config_enabled:
            return "config enabled=false"
        d = self._file()
        if d.get("agents_on") is False:
            return "killswitch.json agents_on=false"
        if d.get("apps", {}).get(self.app) is False:
            return f"killswitch.json apps.{self.app}=false"
        if lane and d.get("lanes", {}).get(f"{self.app}:{lane}") is False:
            return f"killswitch.json lanes.{self.app}:{lane}=false"
        return None

    def is_on(self, lane: Optional[str] = None) -> bool:
        return self.reason_off(lane) is None
