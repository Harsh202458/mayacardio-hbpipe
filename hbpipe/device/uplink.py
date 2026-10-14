"""Cloud link: HTTP client, online/offline mode manager, and the uploader.

Mode rules (docs/PLAN.md §6.4): health ping every 1 s with a hard 1.5 s deadline;
3 consecutive failures -> offline (~5.5 s worst case), 2 consecutive successes -> online.
The collector never depends on the mode, so a switch cannot lose or duplicate samples.
"""
from __future__ import annotations

import gzip
import http.client
import json
import logging
import threading
import time
import urllib.parse
from typing import Optional

from ..common.chunkdb import get_meta, set_meta
from .events import EventLog
from .store import DeviceStore

log = logging.getLogger("hbpipe.uplink")

PING_INTERVAL = 1.0
PING_TIMEOUT = 1.5
FAILS_TO_OFFLINE = 3
OKS_TO_ONLINE = 2
UPLOAD_INTERVAL = 0.5


def bounded(fn, timeout: float):
    """Run fn in a helper thread; give up after `timeout` (covers DNS hangs that ignore
    socket timeouts). Returns (ok, value)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - any failure means "cloud not reachable"
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "error" in box:
        return False, box.get("error")
    return True, box.get("value")


class CloudClient:
    def __init__(self, url: str, token: str):
        u = urllib.parse.urlsplit(url)
        self.https = u.scheme == "https"
        self.host = u.hostname or ""
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.token = token
        self.bytes_sent = 0
        self.bytes_received = 0

    def _conn(self, timeout: float):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)

    def call(self, method: str, path: str, body: Optional[bytes] = None, timeout: float = 5.0,
             headers: Optional[dict] = None):
        h = {"Authorization": f"Bearer {self.token}"}
        if headers:
            h.update(headers)
        conn = self._conn(timeout)
        try:
            conn.request(method, self.prefix + path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            self.bytes_sent += len(body or b"") + 200
            self.bytes_received += len(data) + 200
            if resp.status != 200:
                raise OSError(f"cloud returned HTTP {resp.status}: {data[:200]!r}")
            return data
        finally:
            conn.close()

    def stream(self, path: str, timeout: float = 60.0):
        """Yield response lines (for exports)."""
        conn = self._conn(timeout)
        conn.request("GET", self.prefix + path, headers={"Authorization": f"Bearer {self.token}"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"cloud returned HTTP {resp.status}")
        try:
            for line in resp:
                yield line.decode("utf-8")
        finally:
            conn.close()

    def ping(self) -> bool:
        ok, _ = bounded(lambda: self.call("GET", "/health", timeout=PING_TIMEOUT), PING_TIMEOUT)
        return ok

    def ingest(self, payload: dict, timeout: float):
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)
        ok, value = bounded(lambda: self.call("POST", "/ingest", body, timeout=timeout,
                                              headers={"Content-Type": "application/json",
                                                       "Content-Encoding": "gzip"}),
                            timeout + 1.0)
        if not ok:
            return None
        return json.loads(value)


class ModeManager:
    def __init__(self, client: CloudClient, store: DeviceStore, events: EventLog,
                 stop: threading.Event):
        self.client = client
        self.store = store
        self.events = events
        self.stop = stop
        self.conn = store.new_connection()
        self.lock = threading.Lock()
        # Resume the mode we had before a restart; pings correct it within seconds.
        self.mode = get_meta(self.conn, "mode", "online")
        self.fails = 0
        self.oks = 0
        self._persist("startup")

    def _persist(self, reason: str) -> None:
        set_meta(self.conn, "mode", self.mode)
        set_meta(self.conn, "mode_since", time.time())
        self.events.write("mode", mode=self.mode, reason=reason)
        log.info("mode -> %s (%s)", self.mode, reason)

    def report(self, ok: bool, source: str) -> None:
        with self.lock:
            if ok:
                self.oks += 1
                self.fails = 0
                if self.mode == "offline" and self.oks >= OKS_TO_ONLINE:
                    self.mode = "online"
                    self._persist(f"{source}: {self.oks} successes")
            else:
                self.fails += 1
                self.oks = 0
                if self.mode == "online" and self.fails >= FAILS_TO_OFFLINE:
                    self.mode = "offline"
                    self._persist(f"{source}: {self.fails} failures")

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            self.report(self.client.ping(), "health")
            self.stop.wait(max(0.0, PING_INTERVAL - (time.monotonic() - started)))


class Uploader:
    def __init__(self, cfg, client: CloudClient, store: DeviceStore, mode: ModeManager,
                 stop: threading.Event):
        self.cfg = cfg
        self.client = client
        self.store = store
        self.mode = mode
        self.stop = stop
        self.conn = store.new_connection()

    def run(self) -> None:
        last_meta = 0.0
        while not self.stop.is_set():
            if self.mode.mode != "online":
                self.stop.wait(0.2)
                continue
            batch = self.store.build_upload(self.conn)
            if not (batch["chunks"] or batch["losses"] or batch["sessions"] or batch["results"]):
                self.stop.wait(UPLOAD_INTERVAL)
                continue
            payload = DeviceStore.upload_payload(self.cfg.device_id, batch)
            timeout = 5.0 if not batch["full"] else 30.0
            resp = self.client.ingest(payload, timeout)
            if resp is None or not resp.get("ok"):
                self.mode.report(False, "ingest")
                self.stop.wait(UPLOAD_INTERVAL)
                continue
            self.mode.report(True, "ingest")
            self.store.mark_uploaded(self.conn, batch, resp.get("done_upto", {}))
            now = time.monotonic()
            if now - last_meta > 5:
                set_meta(self.conn, "net_tx_bytes", self.client.bytes_sent)
                set_meta(self.conn, "net_rx_bytes", self.client.bytes_received)
                last_meta = now
            if not batch["full"]:
                self.stop.wait(UPLOAD_INTERVAL)
