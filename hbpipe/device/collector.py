"""Reads both sensors from the mock and saves every sample durably.

Rules (see docs/PLAN.md §4):
  * every request carries from_index=cursor once the session is known, so restarts,
    duplicates and corrupt replies are harmless re-reads;
  * every received line is applied on its own merits (sensor, indices), never by pairing;
  * an unexplained gap is re-read; it is recorded lost only after GAP_RETRIES failed re-reads;
  * nothing is ever invented: indices are either stored samples or recorded losses.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from typing import Optional

from ..common.codec import CHANNELS, MAX_CHUNK, pack
from ..common.protocol import CorruptReply, ProtocolError, Reply, parse_reply, read_request
from .events import EventLog
from .store import Batch, DeviceStore, is_disk_full

log = logging.getLogger("hbpipe.collector")

GAP_RETRIES = 5
TIMEOUTS_BEFORE_RECONNECT = 3
SENSORS = tuple(sorted(CHANNELS))


def read_boot_id() -> str:
    """Identifies the current boot. HB_BOOT_ID overrides it (containers share the host kernel's
    boot id, so the container entry script sets a fresh one per container start)."""
    if os.environ.get("HB_BOOT_ID"):
        return os.environ["HB_BOOT_ID"]
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


class SensorState:
    __slots__ = ("cursor", "ended", "gap_at", "gap_tries", "last_loss")

    def __init__(self, cursor: int = 0, ended: bool = False):
        self.cursor = cursor
        self.ended = ended
        self.gap_at: Optional[int] = None
        self.gap_tries = 0
        self.last_loss = None  # (from, to, reason) of the most recent loss, for merging


class Collector:
    def __init__(self, cfg, store: DeviceStore, events: EventLog, stop: threading.Event):
        self.cfg = cfg
        self.store = store
        self.events = events
        self.stop = stop
        self.boot_id = read_boot_id()
        self.batch = Batch()
        self.sock: Optional[socket.socket] = None
        self.buf = bytearray()
        self.disk_full = False
        self.reserve_used = False
        # Set once both sensors return less than a full reply: the mock's buffer has been
        # drained after a (re)start, so other work may now compete for the CPU.
        self.caught_up = threading.Event()
        self._fresh: set = set()
        self._queue: queue.Queue = queue.Queue()   # batches waiting for the writer, in order
        self._writer: Optional[threading.Thread] = None
        self.writer_error: Optional[BaseException] = None
        self.stats = {"replies": 0, "corrupt": 0, "duplicates": 0, "gap_rereads": 0,
                      "bad_lines": 0, "reconnects": 0}

        sid, state, session_boot, known = store.load_collector_state()
        self.known = set(known)
        self.session: Optional[str] = sid
        self.session_boot = session_boot
        self.state = {s: SensorState(*state.get(s, (0, False))) for s in SENSORS}

    # ================================================================ main loop
    def run(self) -> None:
        # No room for the reserve (disk already full at start): run in the low-space stage.
        self.reserve_used = not self.store.ensure_reserve(self.cfg.reserve_mb)
        # Commits run on their own thread: a slow fsync must never stop us reading the mock,
        # whose buffer only holds 10 s.
        self._writer = threading.Thread(target=self._writer_loop, name="writer", daemon=True)
        self._writer.start()
        backoff = 0.2
        next_flush = time.monotonic() + self.cfg.commit_interval
        next_disk_check = 0.0
        while not self.stop.is_set():
            try:
                if self.sock is None:
                    self._connect()
                    backoff = 0.2
                self._poll_round()
            except OSError as e:
                log.warning("mock connection problem: %s", e)
                self._disconnect()
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 2.0)
            now = time.monotonic()
            if now >= next_disk_check:
                self._check_disk()
                next_disk_check = now + 1.0
            if now >= next_flush:
                self.flush()
                next_flush = now + self.cfg.commit_interval
        self.flush()
        self._disconnect()
        self._queue.put(None)
        self._writer.join()
        if self.writer_error is not None:
            raise self.writer_error

    def _connect(self) -> None:
        s = socket.create_connection((self.cfg.mock_host, self.cfg.mock_port), timeout=2.0)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.buf.clear()
        self.timeouts = 0
        self.next_poll = {s_: 0.0 for s_ in SENSORS}
        log.info("connected to mock %s:%s", self.cfg.mock_host, self.cfg.mock_port)

    def _disconnect(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.stats["reconnects"] += 1

    def _poll_round(self) -> None:
        now = time.monotonic()
        for sensor in SENSORS:
            if now < self.next_poll[sensor]:
                continue
            got = self._request(sensor)
            if got is None:
                self.timeouts += 1
                if self.timeouts >= TIMEOUTS_BEFORE_RECONNECT:
                    raise OSError("mock not answering")
                self.next_poll[sensor] = time.monotonic() + self.cfg.poll_interval
                continue
            self.timeouts = 0
            if got < MAX_CHUNK and not self.caught_up.is_set():
                self._fresh.add(sensor)
                if len(self._fresh) == len(SENSORS):
                    self.caught_up.set()
            st = self.state[sensor]
            if got >= MAX_CHUNK or st.gap_at is not None:
                delay = 0.0                       # catching up or re-reading: ask again now
            elif st.ended:
                delay = 1.0                       # only waiting to discover a new session
            else:
                delay = self.cfg.poll_interval
            self.next_poll[sensor] = time.monotonic() + delay
        wait = min(self.next_poll.values()) - time.monotonic()
        if wait > 0:
            self.stop.wait(min(wait, self.cfg.commit_interval))

    def _request(self, sensor: int) -> Optional[int]:
        """Send one read; process lines until a reply for this sensor arrives.

        Returns the number of samples in that reply (0 for corrupt), or None on timeout."""
        st = self.state[sensor]
        from_index = st.cursor if (self.session is not None and not st.ended) else None
        self.sock.settimeout(self.cfg.reply_timeout)
        self.sock.sendall(read_request(sensor, from_index))
        deadline = time.monotonic() + self.cfg.reply_timeout
        while True:
            line = self._readline(deadline)
            if line is None:
                return None
            rep_sensor, count = self.handle_line(line)
            if rep_sensor == sensor:
                return count

    def _readline(self, deadline: float) -> Optional[bytes]:
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl])
                del self.buf[:nl + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise OSError("mock closed the connection")
            self.buf += chunk
            if len(self.buf) > 8 * 1024 * 1024:
                raise OSError("reply too large")

    # ================================================================ reply handling
    def handle_line(self, line: bytes):
        """Apply one line. Returns (sensor, sample count) or (None, 0) for unusable lines."""
        try:
            rep = parse_reply(line)
        except CorruptReply as e:
            self.stats["corrupt"] += 1
            log.info("corrupt reply for sensor %s discarded (%s); will re-read", e.sensor, e)
            return e.sensor, 0
        except ProtocolError as e:
            self.stats["bad_lines"] += 1
            log.warning("unusable line from mock: %s", e)
            return None, 0
        self.stats["replies"] += 1
        self.apply(rep)
        return rep.sensor, rep.count

    def apply(self, rep: Reply) -> None:
        if rep.session_id != self.session:
            if rep.session_id in self.known:
                self.stats["duplicates"] += 1
                return  # late reply from a session that is already closed
            self._start_session(rep)

        st = self.state[rep.sensor]
        if st.ended:
            return
        events = [(l.from_index, l.to_index, "loss", l.reason)
                  for l in rep.lost if l.to_index is not None]
        if rep.count:
            events.append((rep.first_index, rep.first_index + rep.count - 1, "data", rep.data))
        events.sort(key=lambda e: (e[0], e[2] == "data"))

        blocked = False
        for start, end, kind, payload in events:
            if end < st.cursor:
                self.stats["duplicates"] += kind == "data"
                continue
            if start > st.cursor and not self._gap(rep.sensor, st, start):
                blocked = True  # re-read from the cursor before going further
                break
            c = st.cursor
            if kind == "loss":
                self._record_loss(rep.sensor, c, end, payload)
            else:
                self._save(rep.sensor, c, payload[c - start:])
            st.cursor = end + 1
        if not blocked:
            st.gap_at, st.gap_tries = None, 0
        self.batch.cursors[(self.session, rep.sensor)] = st.cursor

        if not blocked and rep.session_end and st.cursor >= rep.first_index + rep.count:
            st.ended = True
            self.batch.ends[(self.session, rep.sensor)] = st.cursor
            self.events.write("sensor_end", session=self.session, sensor=rep.sensor,
                              end_index=st.cursor)

    def _gap(self, sensor: int, st: SensorState, start: int) -> bool:
        """An index jump the mock did not explain. Re-read; give up after GAP_RETRIES."""
        if st.gap_at == st.cursor:
            st.gap_tries += 1
        else:
            st.gap_at, st.gap_tries = st.cursor, 1
        self.stats["gap_rereads"] += 1
        if st.gap_tries < GAP_RETRIES:
            return False
        self._record_loss(sensor, st.cursor, start - 1, "not_delivered_by_mock")
        st.cursor = start
        st.gap_at, st.gap_tries = None, 0
        return True

    def _start_session(self, rep: Reply) -> None:
        old = self.session
        if old is not None:
            if self.session_boot not in (None, "unknown") and self.session_boot != self.boot_id:
                reason = "device_power_loss_or_reboot"
            else:
                reason = "mock_restart"
            mock_reasons = [l.reason for l in rep.lost if l.to_index is None]
            if mock_reasons:
                reason = mock_reasons[0]
            for s, st in self.state.items():
                if not st.ended:
                    self._record_loss(s, st.cursor, None, reason)
                    st.ended = True
                    self.batch.ends[(old, s)] = st.cursor
                    self.batch.cursors[(old, s)] = st.cursor
            self.events.write("session_closed", session=old, reason=reason,
                              cursors={s: st.cursor for s, st in self.state.items()})
        self.session = rep.session_id
        self.session_boot = self.boot_id
        self.known.add(rep.session_id)
        self.state = {s: SensorState() for s in SENSORS}
        self.batch.new_sessions.append((rep.session_id, self.boot_id))
        self.batch.meta["current_session"] = rep.session_id
        for s in SENSORS:
            self.batch.cursors[(rep.session_id, s)] = 0
        self.events.write("session_started", session=rep.session_id)
        log.info("new mock session %s", rep.session_id)

    # ================================================================ persistence
    def _record_loss(self, sensor: int, frm: int, to: Optional[int], reason: str,
                     session: Optional[str] = None) -> None:
        sid = session or self.session
        st = self.state[sensor]
        last = st.last_loss
        if last and last[2] == reason and last[1] is not None and last[1] + 1 == frm:
            frm = last[0]  # extend the previous loss record instead of adding a new one
        st.last_loss = (frm, to, reason)
        self.batch.losses[(sid, sensor, frm)] = (to, reason)
        if reason != "disk_full" or frm == to:
            self.events.write("loss", session=sid, sensor=sensor, from_index=frm,
                              to_index=to, reason=reason)

    def _save(self, sensor: int, start: int, rows: list) -> None:
        if not rows:
            return
        if self.disk_full:
            self._record_loss(sensor, start, start + len(rows) - 1, "disk_full")
            return
        st = self.state[sensor]
        st.last_loss = None
        for off in range(0, len(rows), MAX_CHUNK):
            part = rows[off:off + MAX_CHUNK]
            self.batch.chunks.append((self.session, sensor, start + off, len(part), pack(part)))

    def flush(self) -> None:
        """Hand the current batch to the writer (or commit it here when no writer runs)."""
        if self.batch.empty():
            return
        batch, self.batch = self.batch, Batch()
        if self._writer is None:
            self._commit(batch)
        else:
            self._queue.put(batch)

    def _writer_loop(self) -> None:
        while True:
            batch = self._queue.get()
            if batch is None:
                return
            try:
                self._commit(batch)
            except BaseException as e:  # noqa: BLE001 - stop the pipeline; systemd restarts it
                log.exception("commit failed")
                self.writer_error = e
                self.stop.set()
                return

    def _commit(self, batch: Batch) -> None:
        """Commit one batch atomically. Batches are committed strictly in order."""
        try:
            self.store.commit(batch)
            return
        except Exception as e:
            if not is_disk_full(e):
                raise
            log.error("disk full while committing: %s", e)
        # Free the reserve and retry: normally the pending samples still fit.
        self._use_reserve()
        try:
            self.store.commit(batch)
            return
        except Exception as e:
            if not is_disk_full(e):
                raise
        # Still no room: keep the cursors and record the samples as lost instead.
        self._enter_disk_full()
        lossy = Batch(new_sessions=batch.new_sessions, losses=dict(batch.losses),
                      cursors=batch.cursors, ends=batch.ends, meta=dict(batch.meta, disk_full=1))
        for sid, sensor, first, count, _blob in batch.chunks:
            lossy.losses[(sid, sensor, first)] = (first + count - 1, "disk_full")
        try:
            self.store.commit(lossy)
        except Exception as e:
            if not is_disk_full(e):
                raise
            # Nothing could be written. Nothing was acknowledged either: on restart we
            # re-read from the last committed cursor and the mock reports what it no longer has.
            log.critical("cannot write even loss records; dropping uncommitted state")
            raise SystemExit(75)

    # Disk space, in two stages:
    #   1. low space   -> release the reserve file and keep saving samples into that space;
    #   2. almost none -> stop saving samples, record them as `disk_full` losses (tiny rows).
    # Both stages end when space returns and the reserve can be recreated.
    def _use_reserve(self) -> None:
        if not self.reserve_used:
            self.reserve_used = True
            self.store.release_reserve()
            self.events.write("disk_low", free_mb=round(self.store.free_mb(), 1))

    def _enter_disk_full(self) -> None:
        self._use_reserve()
        if not self.disk_full:
            self.disk_full = True
            self.batch.meta["disk_full"] = 1
            self.events.write("disk_full", free_mb=round(self.store.free_mb(), 1))

    def _check_disk(self) -> None:
        try:
            free = self.store.free_mb()
        except OSError:
            return
        if not self.reserve_used and free < self.cfg.low_water_mb:
            self._use_reserve()
        elif self.reserve_used and not self.disk_full and free < self.cfg.hard_min_mb:
            self._enter_disk_full()
        elif self.reserve_used and free > self.cfg.reserve_mb + self.cfg.low_water_mb + 8:
            if self.store.ensure_reserve(self.cfg.reserve_mb):
                self.reserve_used = False
                if self.disk_full:
                    self.disk_full = False
                    self.batch.meta["disk_full"] = 0
                self.events.write("disk_recovered", free_mb=round(free, 1))
