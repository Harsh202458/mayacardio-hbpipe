"""Cloud database: SQLite (file path) or PostgreSQL (postgres:// URL, e.g. Neon free tier).

All cloud SQL is written once with `?` placeholders and `ON CONFLICT` clauses, which both
engines accept; the Postgres wrapper only translates the placeholder style.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager

from ..common import chunkdb

log = logging.getLogger("hbcloud.db")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL, first_index INTEGER NOT NULL,
    count INTEGER NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY(session_id, sensor, first_index));
CREATE TABLE IF NOT EXISTS losses(
    session_id TEXT NOT NULL, sensor INTEGER NOT NULL, from_index INTEGER NOT NULL,
    to_index INTEGER, reason TEXT NOT NULL,
    PRIMARY KEY(session_id, sensor, from_index));
CREATE TABLE IF NOT EXISTS sessions(
    session_id TEXT PRIMARY KEY, device TEXT, end_1 INTEGER, end_2 INTEGER);
CREATE TABLE IF NOT EXISTS results(
    session_id TEXT NOT NULL, win INTEGER NOT NULL, mode TEXT NOT NULL, body TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(session_id, win, mode));
"""

PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS chunks(
        session_id TEXT NOT NULL, sensor SMALLINT NOT NULL, first_index BIGINT NOT NULL,
        count INTEGER NOT NULL, data BYTEA NOT NULL,
        PRIMARY KEY(session_id, sensor, first_index))""",
    """CREATE TABLE IF NOT EXISTS losses(
        session_id TEXT NOT NULL, sensor SMALLINT NOT NULL, from_index BIGINT NOT NULL,
        to_index BIGINT, reason TEXT NOT NULL,
        PRIMARY KEY(session_id, sensor, from_index))""",
    """CREATE TABLE IF NOT EXISTS sessions(
        session_id TEXT PRIMARY KEY, device TEXT, end_1 BIGINT, end_2 BIGINT)""",
    """CREATE TABLE IF NOT EXISTS results(
        session_id TEXT NOT NULL, win INTEGER NOT NULL, mode TEXT NOT NULL, body TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY(session_id, win, mode))""",
]


class PgConn:
    """Minimal sqlite3-like facade over a psycopg 3 connection, reconnecting when Neon closes
    idle connections."""

    def __init__(self, url: str):
        import psycopg  # only needed for the Postgres backend
        self._psycopg = psycopg
        self.url = url
        self.c = None
        self._connect()

    def _connect(self):
        if self.c is not None:
            try:
                self.c.close()
            except Exception:  # noqa: BLE001
                pass
        # prepare_threshold=None: no server-side prepared statements, so a pooled (PgBouncer)
        # Neon connection string works as well as a direct one.
        self.c = self._psycopg.connect(self.url, autocommit=True, connect_timeout=10,
                                       prepare_threshold=None)

    def _ensure(self):
        if self.c is None or self.c.closed or self.c.broken:
            self._connect()

    def execute(self, sql: str, params=()):
        self._ensure()
        q = sql.replace("?", "%s")
        try:
            return self.c.execute(q, params)
        except self._psycopg.OperationalError:
            if self.c.info.transaction_status != self._psycopg.pq.TransactionStatus.IDLE:
                raise  # inside a transaction: let the caller roll back and retry
            log.warning("database connection lost; reconnecting")
            self._connect()
            return self.c.execute(q, params)

    def executemany(self, sql: str, seq):
        seq = list(seq)
        if not seq:
            return
        self._ensure()
        with self.c.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), seq)

    @contextmanager
    def transaction(self):
        self._ensure()
        with self.c.transaction():
            yield self

    def close(self):
        if self.c is not None:
            self.c.close()


class Database:
    def __init__(self, url: str):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        conn = self.connect()
        if self.pg:
            for stmt in PG_SCHEMA:
                conn.execute(stmt)
        else:
            conn.executescript(SQLITE_SCHEMA)
        conn.close()

    def connect(self, readonly: bool = False):
        if self.pg:
            return PgConn(self.url)
        return chunkdb.connect(self.url, readonly=readonly)

    def transaction(self, conn):
        if self.pg:
            return conn.transaction()
        return chunkdb.transaction(conn)

    def describe(self) -> str:
        if self.pg:
            return "postgres://" + self.url.split("@")[-1].split("?")[0]
        return "sqlite:" + self.url
