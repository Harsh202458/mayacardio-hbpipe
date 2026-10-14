"""Append-only JSON-lines event log (mode switches, sessions, losses) used as test evidence."""
from __future__ import annotations

import json
import os
import threading
import time


class EventLog:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, event: str, **fields) -> None:
        rec = {"event": event, "wall": round(time.time(), 3),
               "mono": round(time.monotonic(), 3)}
        rec.update(fields)
        line = json.dumps(rec, default=str) + "\n"
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # diagnostics must never take the pipeline down (e.g. disk full)
