"""Your approve/reject decisions for non-sports market pairs, saved in matches.json."""

import json
import threading
from datetime import datetime, timezone

from . import config

PATH = config.PROJECT_ROOT / "matches.json"


class MatchStore:
    def __init__(self, path=PATH):
        self.path, self.lock = path, threading.Lock()
        self.data = {"approved": [], "rejected": [], "rejected_events": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def pair_id(pm, kalshi):
        return f"{pm}|{kalshi}"

    def approved(self):
        with self.lock:
            return list(self.data["approved"])

    def decided(self):
        """Set of (pm, kalshi) pairs already approved or rejected, and rejected event pairs."""
        with self.lock:
            pairs = {(a["pm"], a["kalshi"]) for a in self.data["approved"]}
            pairs |= {(r["pm"], r["kalshi"]) for r in self.data["rejected"]}
            events = {(r["pm_event"], r["kalshi_event"]) for r in self.data["rejected_events"]}
            return pairs, events

    def decide(self, pm, kalshi, relation, **extra):
        """relation: same | opposite | reject | remove."""
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            for key in ("approved", "rejected"):
                self.data[key] = [x for x in self.data[key] if (x["pm"], x["kalshi"]) != (pm, kalshi)]
            if relation in ("same", "opposite"):
                self.data["approved"].append({"pm": pm, "kalshi": kalshi, "relation": relation, "at": now, **extra})
            elif relation == "reject":
                self.data["rejected"].append({"pm": pm, "kalshi": kalshi, "at": now})
            elif relation != "remove":
                raise ValueError("relation must be same, opposite, reject or remove")
            self._save()

    def reject_event(self, pm_event, kalshi_event):
        with self.lock:
            self.data["rejected_events"].append({"pm_event": pm_event, "kalshi_event": kalshi_event,
                                                 "at": datetime.now(timezone.utc).isoformat()})
            self._save()
