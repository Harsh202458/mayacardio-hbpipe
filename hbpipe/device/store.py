"""Device-side durable store (SQLite, WAL, synchronous=FULL).

Invariant: for every (session, sensor), [0, cursor) is covered exactly once by chunks and
losses, and a chunk/loss is always committed in the same transaction as its cursor advance.
"""
from __future__ import annotations

import base64
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..common import chunkdb
from ..common.chunkdb import get_meta, set_meta, transaction
from ..common.intervals import IntervalSet

DEVICE_SCHEMA = chunkdb.COMMON_SCHEMA + """
CREATE TABLE IF NOT EXISTS sessions(
    session_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL,
    boot_id TEXT,
    end_1 INTEGER,
    end_2 INTEGER,
    dev_next_window INTEGER NOT NULL DEFAULT 0,
    cloud_done INTEGER NOT NULL DEFAULT -1,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cursors(
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    next_index INTEGER NOT NULL,
    PRIMARY KEY(session_id, sensor)
);
CREATE TABLE IF NOT EXISTS results(
    session_id TEXT NOT NULL,
    window INTEGER NOT NULL,
    mode TEXT NOT NULL,
    body TEXT NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, window, mode)
);
CREATE INDEX IF NOT EXISTS results_unsent ON results(session_id, window) WHERE uploaded = 0;
CREATE TABLE IF NOT EXISTS acked(       -- chunk ids [id_from, id_to) confirmed by the cloud
    id_from INTEGER PRIMARY KEY,
    id_to INTEGER NOT NULL
);
"""


def _gaps(covered: IntervalSet, lo: int, hi: int) -> list:
    """Half-open ranges within [lo, hi) not covered by `covered`."""
    out, cur = [], lo
    for s, e in zip(covered.starts, covered.ends):
        if e <= cur:
            continue
        if s >= hi:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        out.append((cur, hi))
    return out


def is_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return True
    msg = str(exc).lower()
    return "disk is full" in msg or "database or disk is full" in msg or "no space" in msg


@dataclass
class Batch:
    """Everything collected since the last commit; written atomically."""
    new_sessions: list = field(default_factory=list)       # (session_id, boot_id)
    chunks: list = field(default_factory=list)             # (sid, sensor, first, count, blob)
    losses: dict = field(default_factory=dict)             # (sid, sensor, from) -> (to, reason)
    cursors: dict = field(default_factory=dict)            # (sid, sensor) -> next_index
    ends: dict = field(default_factory=dict)               # (sid, sensor) -> end index
    meta: dict = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.new_sessions or self.chunks or self.losses or self.cursors
                    or self.ends or self.meta)


class DeviceStore:
    def __init__(self, data_dir: str, readonly: bool = False):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "pipeline.db")
        self.reserve_path = os.path.join(data_dir, "reserve.bin")
        if readonly:
            self.conn = chunkdb.connect(self.path, readonly=True)
        else:
            os.makedirs(data_dir, exist_ok=True)
            self.conn = chunkdb.connect(self.path)
            self.conn.executescript(DEVICE_SCHEMA)

    def new_connection(self) -> sqlite3.Connection:
        """Connection for the uploader, mode manager and offline processor. Their commits are
        bookkeeping that is safe to lose in a crash (chunks are re-sent and deduplicated by the
        cloud; results are recomputed), so they skip the fsync; WAL keeps them consistent.
        Only the collector's connection (self.conn) commits with synchronous=FULL."""
        conn = chunkdb.connect(self.path)
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---------------------------------------------------------------- collector side
    def commit(self, b: Batch) -> None:
        c = self.conn
        with transaction(c):
            for sid, boot_id in b.new_sessions:
                c.execute("INSERT OR IGNORE INTO sessions(session_id, seq, boot_id) VALUES("
                          "?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM sessions), ?)", (sid, boot_id))
            c.executemany("INSERT OR IGNORE INTO chunks(session_id, sensor, first_index, count, data)"
                          " VALUES(?,?,?,?,?)", b.chunks)
            c.executemany(
                "INSERT INTO losses(session_id, sensor, from_index, to_index, reason) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_id, sensor, from_index) DO UPDATE SET "
                "to_index=excluded.to_index, reason=excluded.reason, uploaded=0",
                [(sid, s, frm, to, reason) for (sid, s, frm), (to, reason) in b.losses.items()])
            c.executemany(
                "INSERT INTO cursors(session_id, sensor, next_index) VALUES(?,?,?) "
                "ON CONFLICT(session_id, sensor) DO UPDATE SET next_index=excluded.next_index",
                [(sid, s, n) for (sid, s), n in b.cursors.items()])
            for (sid, s), end in b.ends.items():
                c.execute(f"UPDATE sessions SET end_{int(s)}=?, uploaded=0 WHERE session_id=?",
                          (end, sid))
            for k, v in b.meta.items():
                set_meta(c, k, v)

    def load_collector_state(self):
        """(current_session, {sensor: (cursor, ended)}, boot_id of that session, known sessions)."""
        c = self.conn
        known = {r[0] for r in c.execute("SELECT session_id FROM sessions")}
        sid = get_meta(c, "current_session")
        if sid is None or sid not in known:
            return None, {}, None, known
        row = c.execute("SELECT boot_id, end_1, end_2 FROM sessions WHERE session_id=?",
                        (sid,)).fetchone()
        cursors = dict(c.execute("SELECT sensor, next_index FROM cursors WHERE session_id=?",
                                 (sid,)).fetchall())
        state = {1: (cursors.get(1, 0), row[1] is not None),
                 2: (cursors.get(2, 0), row[2] is not None)}
        return sid, state, row[0], known

    # ---------------------------------------------------------------- disk-full reserve
    def ensure_reserve(self, size_mb: int) -> bool:
        size = size_mb * 1024 * 1024
        try:
            if os.path.exists(self.reserve_path) and os.path.getsize(self.reserve_path) >= size:
                return True
            with open(self.reserve_path, "wb") as f:
                if hasattr(os, "posix_fallocate"):
                    os.posix_fallocate(f.fileno(), 0, size)
                else:
                    block = b"\0" * (1024 * 1024)
                    for _ in range(size_mb):
                        f.write(block)
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            self.release_reserve()
            return False

    def release_reserve(self) -> None:
        try:
            os.remove(self.reserve_path)
        except OSError:
            pass

    def free_mb(self) -> float:
        return shutil.disk_usage(self.data_dir).free / (1024 * 1024)

    # ---------------------------------------------------------------- uploader side
    def build_upload(self, conn, live_chunks=600, backlog_chunks=2400, max_results=2000):
        sessions = conn.execute(
            "SELECT session_id, end_1, end_2 FROM sessions WHERE uploaded=0").fetchall()
        losses = conn.execute(
            "SELECT session_id, sensor, from_index, to_index, reason FROM losses WHERE uploaded=0 "
            "LIMIT 5000").fetchall()
        results = conn.execute(
            "SELECT session_id, window, mode, body FROM results WHERE uploaded=0 "
            "ORDER BY session_id, window LIMIT ?", (max_results,)).fetchall()
        more_results = len(results) == max_results

        # Unsent chunks = chunk ids not yet in the acknowledged ranges.
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[0]
        gaps = _gaps(self._acked(conn), 1, max_id + 1)
        live, need = [], live_chunks          # newest first: keeps online results flowing
        for a, b in reversed(gaps):
            take = min(need, b - a)
            live.append((b - take, b))
            need -= take
            if need == 0:
                break
        backlog = []
        if not more_results and need == 0:
            # Offline results are sent before older data, so the cloud never recomputes
            # windows the device already processed.
            limit, need = live[-1][0], backlog_chunks
            for a, b in gaps:
                if a >= limit or need == 0:
                    break
                take = min(need, min(b, limit) - a)
                backlog.append((a, a + take))
                need -= take
        ranges = backlog + sorted(live)
        chunks = []
        for a, b in ranges:
            chunks += conn.execute(
                "SELECT id, session_id, sensor, first_index, count, data FROM chunks "
                "WHERE id >= ? AND id < ? ORDER BY id", (a, b)).fetchall()
        return {"sessions": sessions, "losses": losses, "results": results, "chunks": chunks,
                "chunk_ranges": ranges, "full": bool(backlog) or more_results}

    @staticmethod
    def _acked(conn) -> IntervalSet:
        acked = IntervalSet()
        for a, b in conn.execute("SELECT id_from, id_to FROM acked"):
            acked.add(a, b)
        return acked

    @staticmethod
    def upload_payload(device_id: str, batch: dict) -> dict:
        return {
            "device": device_id,
            "sessions": [{"session_id": s, "end_1": e1, "end_2": e2}
                         for s, e1, e2 in batch["sessions"]],
            "losses": [list(r) for r in batch["losses"]],
            "results": [{"session_id": s, "window": w, "mode": m, "body": body}
                        for s, w, m, body in batch["results"]],
            "chunks": [[sid, sensor, first, count, base64.b64encode(blob).decode("ascii")]
                       for _id, sid, sensor, first, count, blob in batch["chunks"]],
        }

    def mark_uploaded(self, conn, batch: dict, done_upto: dict) -> None:
        with transaction(conn):
            # Chunk rows are never rewritten: acknowledged ids are kept as a few merged ranges.
            acked = self._acked(conn)
            for a, b in batch["chunk_ranges"]:
                acked.add(a, b)
            conn.execute("DELETE FROM acked")
            conn.executemany("INSERT INTO acked(id_from, id_to) VALUES(?, ?)",
                             list(zip(acked.starts, acked.ends)))
            # Conditional: if a loss/session changed after we read it, it stays un-uploaded.
            conn.executemany(
                "UPDATE losses SET uploaded=1 WHERE session_id=? AND sensor=? AND from_index=? "
                "AND to_index IS ? AND reason=?", batch["losses"])
            conn.executemany(
                "UPDATE sessions SET uploaded=1 WHERE session_id=? AND end_1 IS ? AND end_2 IS ?",
                batch["sessions"])
            conn.executemany(
                "UPDATE results SET uploaded=1 WHERE session_id=? AND window=? AND mode=?",
                [(s, w, m) for s, w, m, _ in batch["results"]])
            for sid, done in done_upto.items():
                conn.execute("UPDATE sessions SET cloud_done=MAX(cloud_done, ?) WHERE session_id=?",
                             (int(done), sid))

    # ---------------------------------------------------------------- queries
    def sessions(self, conn=None):
        conn = conn or self.conn
        return conn.execute("SELECT session_id, seq, end_1, end_2, dev_next_window, cloud_done "
                            "FROM sessions ORDER BY seq").fetchall()

    def resolve_session(self, sid: str) -> Optional[str]:
        if sid != "latest":
            return sid
        row = self.conn.execute("SELECT session_id FROM sessions ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def counts(self) -> dict:
        saved = self.conn.execute("SELECT COALESCE(SUM(count), 0) FROM chunks").fetchone()[0]
        in_cloud = 0
        for a, b in self.conn.execute("SELECT id_from, id_to FROM acked").fetchall():
            in_cloud += self.conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM chunks WHERE id >= ? AND id < ?",
                (a, b)).fetchone()[0]
        losses = self.conn.execute("SELECT COUNT(*) FROM losses").fetchone()[0]
        return {"samples_saved": saved, "samples_in_cloud": in_cloud,
                "backlog": saved - in_cloud, "losses": losses}
