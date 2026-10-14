"""SQLite helpers shared by device and cloud. Both stores use the same `chunks` and `losses`
layout, so loading, export and result formatting are written once."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from .codec import CHANNELS, MAX_CHUNK, unpack_flat

COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    first_index INTEGER NOT NULL,
    count INTEGER NOT NULL,
    data BLOB NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, sensor, first_index)
);
CREATE INDEX IF NOT EXISTS chunks_unsent ON chunks(id) WHERE uploaded = 0;
CREATE TABLE IF NOT EXISTS losses(
    session_id TEXT NOT NULL,
    sensor INTEGER NOT NULL,
    from_index INTEGER NOT NULL,
    to_index INTEGER,
    reason TEXT NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, sensor, from_index)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                               check_same_thread=False, timeout=30)
    else:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_range(conn: sqlite3.Connection, session: str, sensor: int, lo: int, hi: int) -> list:
    """Samples [lo, hi] of a sensor: tuple of channel values, or None when not stored."""
    out = [None] * (hi - lo + 1)
    if hi < lo:
        return out
    ch = CHANNELS[sensor]
    rows = conn.execute(
        "SELECT first_index, count, data FROM chunks WHERE session_id=? AND sensor=? "
        "AND first_index BETWEEN ? AND ? ORDER BY first_index",
        (session, sensor, lo - MAX_CHUNK + 1, hi))
    for first, count, blob in rows:
        flat = unpack_flat(blob)
        a = max(lo, first)
        b = min(hi, first + count - 1)
        for idx in range(a, b + 1):
            j = (idx - first) * ch
            out[idx - lo] = tuple(flat[j:j + ch])
    return out


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def export_lines(conn: sqlite3.Connection, session: str):
    """Yield export lines: every stored sample (by sensor, index), then every loss."""
    for sensor in sorted(CHANNELS):
        ch = CHANNELS[sensor]
        rows = conn.execute(
            "SELECT first_index, count, data FROM chunks WHERE session_id=? AND sensor=? "
            "ORDER BY first_index", (session, sensor))
        for first, count, blob in rows:
            flat = unpack_flat(blob)
            for k in range(count):
                vals = ", ".join(str(v) for v in flat[k * ch:(k + 1) * ch])
                yield f'{{"sensor": {sensor}, "index": {first + k}, "values": [{vals}]}}\n'
    rows = conn.execute(
        "SELECT sensor, from_index, to_index, reason FROM losses WHERE session_id=? "
        "ORDER BY sensor, from_index", (session,))
    for sensor, frm, to, reason in rows:
        yield json.dumps({"sensor": sensor, "from_index": frm, "to_index": to,
                          "reason": reason}) + "\n"


def result_line(session: str, body: dict) -> str:
    out = {"session": session}
    out.update(body)
    return json.dumps(out) + "\n"
