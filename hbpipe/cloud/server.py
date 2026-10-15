"""Cloud API (stdlib only). Run behind Caddy for HTTPS.

    HB_CLOUD_TOKEN=... python3 -m hbpipe.cloud.server --db /var/lib/hbcloud/cloud.db --port 8080
"""
from __future__ import annotations

import argparse
import gzip
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common import chunkdb
from .store import CloudStore

log = logging.getLogger("hbcloud")
MAX_BODY = 64 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store: CloudStore = None
    token: str = ""

    def log_message(self, fmt, *args):  # quieter than the default stderr spam
        if os.environ.get("HB_HTTP_LOG"):
            log.info("%s %s", self.address_string(), fmt % args)

    def _authorised(self) -> bool:
        got = self.headers.get("Authorization", "")
        if self.token and not hmac.compare_digest(got, f"Bearer {self.token}"):
            self._send(401, {"error": "unauthorised"})
            return False
        return True

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, lines) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        buf = []
        for line in lines:
            buf.append(line)
            if len(buf) >= 2000:
                self.wfile.write("".join(buf).encode())
                buf.clear()
        if buf:
            self.wfile.write("".join(buf).encode())

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/health":
            return self._send(200, {"ok": True, "time": time.time()})
        if u.path == "/":
            return self._send(200, {
                "service": "hbpipe cloud: Never Lose a Heartbeat",
                "public": {"/health": "liveness check"},
                "token_required": ["/ingest (POST)", "/export?session=ID", "/results?session=ID",
                                   "/status?session=ID", "/sessions"],
                "auth": "Authorization: Bearer <HB_CLOUD_TOKEN>",
            })
        if not self._authorised():
            return
        try:
            conn = self.store.db.connect(readonly=True)
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": f"database unavailable: {e}"})
        try:
            sid = q.get("session", "")
            if u.path == "/export":
                return self._stream(chunkdb.export_lines(conn, sid))
            if u.path == "/results":
                return self._stream(self.store.results_lines(conn, sid))
            if u.path == "/status":
                return self._send(200, self.store.status(conn, sid))
            if u.path == "/sessions":
                rows = conn.execute("SELECT session_id, end_1, end_2 FROM sessions").fetchall()
                return self._send(200, [{"session": s, "end_1": a, "end_2": b} for s, a, b in rows])
            self._send(404, {"error": "not found"})
        finally:
            conn.close()

    def do_POST(self):
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not self._authorised():
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "bad length"})
        body = self.rfile.read(length)
        try:
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            payload = json.loads(body)
        except (ValueError, OSError) as e:
            return self._send(400, {"error": f"bad body: {e}"})
        try:
            done = self.store.ingest(payload)
        except (ValueError, KeyError, TypeError) as e:
            log.warning("rejected ingest: %s", e)
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - database trouble: nothing committed, device retries
            log.exception("ingest failed")
            return self._send(503, {"error": f"storage unavailable: {e}"})
        self._send(200, {"ok": True, "done_upto": done})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return  # client went away (network cut): expected, nothing to do
        super().handle_error(request, client_address)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="hbpipe cloud API")
    p.add_argument("--db", default=os.environ.get("HB_CLOUD_DB", "/var/lib/hbcloud/cloud.db"),
                   help="SQLite file path or postgres:// URL")
    p.add_argument("--host", default=os.environ.get("HB_CLOUD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT") or os.environ.get("HB_CLOUD_PORT", "8080")))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    is_pg = args.db.startswith(("postgres://", "postgresql://"))
    if os.environ.get("HB_REQUIRE_POSTGRES") == "1" and not is_pg:
        # Hosted deployments have no persistent disk: never fall back to a throwaway SQLite
        # file. The value itself is not logged, as it may contain a password.
        log.error("HB_CLOUD_DB must be a full postgresql://user:password@host/db URL "
                  "(got a value that is not one); refusing to start")
        return 2
    if not is_pg:
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    store = CloudStore(args.db)
    Handler.store = store
    Handler.token = os.environ.get("HB_CLOUD_TOKEN", "")
    if not Handler.token:
        log.warning("HB_CLOUD_TOKEN not set: API is unauthenticated")
    stop = threading.Event()
    threading.Thread(target=store.worker, args=(stop,), daemon=True, name="worker").start()
    server = Server((args.host, args.port), Handler)
    log.info("listening on %s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
