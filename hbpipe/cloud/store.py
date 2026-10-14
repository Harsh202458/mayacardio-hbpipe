"""Cloud store + online worker.

Exactly-once: device chunks are immutable and keyed (session, sensor, first_index), so
re-sent chunks are ignored. The device is acknowledged only after the commit.

The worker computes window w only when all of its inputs are final in the cloud (data,
recorded loss, or past the session end), so samples still in transit are never counted
as missing. Windows that already have an offline result from the device are skipped.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

from ..common import chunkdb, processing
from ..common.chunkdb import load_range
from ..common.intervals import INF, IntervalSet
from .db import Database

log = logging.getLogger("hbcloud")


class SessionState:
    def __init__(self):
        self.cov = {1: IntervalSet(), 2: IntervalSet()}
        self.ends = {1: None, 2: None}
        self.done = set()   # windows >= next_w that have any result (online or offline)
        self.next_w = 0     # every window below this has a result

    def is_done(self, w: int) -> bool:
        return w < self.next_w or w in self.done

    def mark_done(self, w: int) -> None:
        if w >= self.next_w:
            self.done.add(w)
            while self.next_w in self.done:
                self.done.discard(self.next_w)
                self.next_w += 1


class CloudStore:
    def __init__(self, url: str):
        self.db = Database(url)
        self.conn = self.db.connect()
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.sessions: dict = {}
        self._rebuild()

    def _s(self, sid: str) -> SessionState:
        st = self.sessions.get(sid)
        if st is None:
            st = self.sessions[sid] = SessionState()
        return st

    def _rebuild(self) -> None:
        c = self.conn
        for sid, e1, e2 in c.execute("SELECT session_id, end_1, end_2 FROM sessions").fetchall():
            st = self._s(sid)
            for s, e in ((1, e1), (2, e2)):
                if e is not None:
                    st.ends[s] = e
                    st.cov[s].add(e, INF)
        for sid, s, first, count in c.execute(
                "SELECT session_id, sensor, first_index, count FROM chunks").fetchall():
            self._s(sid).cov[s].add(first, first + count)
        for sid, s, frm, to in c.execute(
                "SELECT session_id, sensor, from_index, to_index FROM losses").fetchall():
            self._s(sid).cov[s].add(frm, INF if to is None else to + 1)
        for sid, w in c.execute("SELECT session_id, win FROM results").fetchall():
            self._s(sid).mark_done(w)
        log.info("database %s: rebuilt state for %d sessions", self.db.describe(), len(self.sessions))

    # ------------------------------------------------------------------ ingest
    def ingest(self, p: dict) -> dict:
        chunks = [(str(c[0]), int(c[1]), int(c[2]), int(c[3]), base64.b64decode(c[4]))
                  for c in p.get("chunks", [])]
        for sid, sensor, first, count, blob in chunks:
            if sensor not in (1, 2) or len(blob) != count * 4 * (3 if sensor == 1 else 2):
                raise ValueError("malformed chunk")
        losses = [(str(l[0]), int(l[1]), int(l[2]), None if l[3] is None else int(l[3]), str(l[4]))
                  for l in p.get("losses", [])]
        sessions = p.get("sessions", [])
        results = [(str(r["session_id"]), int(r["window"]), str(r["mode"]), str(r["body"]))
                   for r in p.get("results", [])]
        now = time.time()
        with self.write_lock:
            c = self.conn
            try:
                with self.db.transaction(c):
                    for s in sessions:
                        c.execute(
                            "INSERT INTO sessions(session_id, device, end_1, end_2) VALUES(?,?,?,?) "
                            "ON CONFLICT(session_id) DO UPDATE SET "
                            "end_1=COALESCE(excluded.end_1, sessions.end_1), "
                            "end_2=COALESCE(excluded.end_2, sessions.end_2)",
                            (s["session_id"], p.get("device"), s.get("end_1"), s.get("end_2")))
                    c.executemany(
                        "INSERT INTO results(session_id, win, mode, body, created_at) "
                        "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                        [r + (now,) for r in results])
                    c.executemany(
                        "INSERT INTO chunks(session_id, sensor, first_index, count, data) "
                        "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING", chunks)
                    c.executemany(
                        "INSERT INTO losses(session_id, sensor, from_index, to_index, reason) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(session_id, sensor, from_index) DO UPDATE SET "
                        "to_index=excluded.to_index, reason=excluded.reason", losses)
            except Exception:
                if self.db.pg:
                    self.conn = self.db.connect()  # drop a possibly broken connection
                raise
        touched = set()
        with self.state_lock:
            for sid, w, _m, _b in results:
                self._s(sid).mark_done(w)
                touched.add(sid)
            for s in sessions:
                st = self._s(s["session_id"])
                touched.add(s["session_id"])
                for k in (1, 2):
                    e = s.get(f"end_{k}")
                    if e is not None:
                        st.ends[k] = e
                        st.cov[k].add(e, INF)
            for sid, sensor, first, count, _ in chunks:
                self._s(sid).cov[sensor].add(first, first + count)
                touched.add(sid)
            for sid, sensor, frm, to, _ in losses:
                self._s(sid).cov[sensor].add(frm, INF if to is None else to + 1)
                touched.add(sid)
            return {sid: self.sessions[sid].next_w - 1 for sid in touched}

    # ------------------------------------------------------------------ online worker
    def worker(self, stop: threading.Event) -> None:
        conn = None
        while not stop.is_set():
            busy = False
            try:
                if conn is None:
                    conn = self.db.connect()
                busy = self._work(conn)
            except Exception:  # noqa: BLE001
                log.exception("online processing failed")
                conn = None
                stop.wait(1.0)
            stop.wait(0.02 if busy else 0.2)

    def _work(self, conn, budget: int = 200) -> bool:
        with self.state_lock:
            snapshot = list(self.sessions.items())
        produced = 0
        for sid, st in snapshot:
            with self.state_lock:
                last = processing.last_window(st.ends[1], st.ends[2])
                top = min(st.cov[1].max_end, st.cov[2].max_end)
                if last is not None:
                    hi = last
                elif top == INF:
                    hi = st.next_w + 20000
                else:
                    hi = int(top // processing.SR) - 1
                todo = [w for w in range(st.next_w, hi + 1)
                        if not st.is_done(w) and processing.window_ready(st.cov[1], st.cov[2], w)]
            for w in todo[:budget - produced]:
                res = processing.compute_window(
                    lambda s, lo, h: load_range(conn, sid, s, lo, h), w, "online")
                res["processed_at"] = round(time.time(), 3)
                conn.execute(
                    "INSERT INTO results(session_id, win, mode, body, created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (sid, w, "online", json.dumps(res), res["processed_at"]))
                with self.state_lock:
                    st.mark_done(w)
                produced += 1
            if produced >= budget:
                return True
        return False

    # ------------------------------------------------------------------ reads
    def results_lines(self, conn, sid: str):
        rows = conn.execute("SELECT body FROM results WHERE session_id=? ORDER BY win, mode",
                            (sid,)).fetchall()
        for (body,) in rows:
            yield chunkdb.result_line(sid, json.loads(body))

    def status(self, conn, sid: str) -> dict:
        n = conn.execute("SELECT COALESCE(SUM(count), 0) FROM chunks WHERE session_id=?",
                         (sid,)).fetchone()[0]
        r = conn.execute("SELECT mode, COUNT(*) FROM results WHERE session_id=? GROUP BY mode",
                         (sid,)).fetchall()
        return {"session": sid, "samples": int(n), "results": {m: int(k) for m, k in r}}
