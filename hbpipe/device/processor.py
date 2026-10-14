"""Offline processing on the device: while offline, compute every window the cloud has not
done, using the same processing code as the cloud, and queue the results for upload."""
from __future__ import annotations

import json
import logging
import threading

from ..common import processing
from ..common.chunkdb import load_range, transaction
from ..common.intervals import PrefixCoverage
from .store import DeviceStore

log = logging.getLogger("hbpipe.processor")

MAX_WINDOWS_PER_TICK = 50


class OfflineProcessor:
    def __init__(self, store: DeviceStore, mode, stop: threading.Event):
        self.store = store
        self.mode = mode
        self.stop = stop
        self.conn = store.new_connection()
        self.finished = set()

    def run(self) -> None:
        while not self.stop.is_set():
            busy = False
            if self.mode.mode == "offline":
                try:
                    busy = self.tick()
                except Exception:  # noqa: BLE001 - keep collecting even if processing fails
                    log.exception("offline processing failed")
            self.stop.wait(0.0 if busy else 0.25)

    def tick(self) -> bool:
        busy = False
        for sid, _seq, end_1, end_2, dev_next, cloud_done in self.store.sessions(self.conn):
            if sid in self.finished:
                continue
            cursors = dict(self.conn.execute(
                "SELECT sensor, next_index FROM cursors WHERE session_id=?", (sid,)).fetchall())
            cov1 = PrefixCoverage(cursors.get(1, 0), end_1 is not None)
            cov2 = PrefixCoverage(cursors.get(2, 0), end_2 is not None)
            last = processing.last_window(end_1, end_2)
            w = max(dev_next, cloud_done + 1)
            done = []
            while len(done) < MAX_WINDOWS_PER_TICK and (last is None or w <= last) \
                    and processing.window_ready(cov1, cov2, w):
                res = processing.compute_window(
                    lambda s, lo, hi: load_range(self.conn, sid, s, lo, hi), w, "offline")
                done.append((sid, w, "offline", json.dumps(res)))
                w += 1
            if done:
                with transaction(self.conn):
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO results(session_id, window, mode, body) "
                        "VALUES(?,?,?,?)", done)
                    self.conn.execute("UPDATE sessions SET dev_next_window=? WHERE session_id=?",
                                      (w, sid))
                busy = busy or len(done) == MAX_WINDOWS_PER_TICK
            if last is not None and w > last:
                self.finished.add(sid)
        return busy
