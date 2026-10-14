"""`pipeline` command: run | status | export | results | sessions."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import urllib.parse

from ..common.chunkdb import export_lines, get_meta, result_line
from .collector import Collector
from .config import Config
from .events import EventLog
from .processor import OfflineProcessor
from .store import DeviceStore
from .uplink import CloudClient, ModeManager, Uploader

log = logging.getLogger("hbpipe")


def cmd_run(cfg: Config, _args) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("signal %s: stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    store = DeviceStore(cfg.data_dir)
    events = EventLog(os.path.join(cfg.data_dir, "events.jsonl"))
    events.write("start", pid=os.getpid())
    collector = Collector(cfg, store, events, stop)
    threads = []
    if cfg.cloud_url:
        client = CloudClient(cfg.cloud_url, cfg.cloud_token)
        mode = ModeManager(client, store, events, stop)
        threads += [threading.Thread(target=mode.run, name="mode", daemon=True),
                    threading.Thread(target=Uploader(cfg, client, store, mode, stop).run,
                                     name="uploader", daemon=True)]
    else:
        log.warning("HB_CLOUD_URL not set: running offline only")
        mode = type("Offline", (), {"mode": "offline"})()
    threads.append(threading.Thread(target=OfflineProcessor(store, mode, stop).run,
                                    name="processor", daemon=True))
    for t in threads:
        t.start()
    try:
        collector.run()  # the collector owns the main thread; it must never wait on the others
    finally:
        stop.set()
    return 0


def _client(cfg: Config) -> CloudClient:
    if not cfg.cloud_url:
        raise SystemExit("HB_CLOUD_URL is not configured")
    return CloudClient(cfg.cloud_url, cfg.cloud_token)


def _session(store: DeviceStore, sid: str) -> str:
    resolved = store.resolve_session(sid)
    if resolved is None:
        raise SystemExit("no sessions recorded yet")
    return resolved


def cmd_status(cfg: Config, _args) -> int:
    store = DeviceStore(cfg.data_dir, readonly=True)
    c = store.conn
    out = {"mode": get_meta(c, "mode", "offline"),
           "session": get_meta(c, "current_session")}
    out.update(store.counts())
    out["disk_full"] = get_meta(c, "disk_full", "0") == "1"
    print(json.dumps(out))
    return 0


def cmd_export(cfg: Config, args) -> int:
    out = sys.stdout
    if args.source == "device":
        store = DeviceStore(cfg.data_dir, readonly=True)
        for line in export_lines(store.conn, _session(store, args.session)):
            out.write(line)
    else:
        sid = args.session
        if sid == "latest":
            sid = _session(DeviceStore(cfg.data_dir, readonly=True), sid)
        for line in _client(cfg).stream("/export?session=" + urllib.parse.quote(sid), 600):
            out.write(line)
    out.flush()
    return 0


def cmd_results(cfg: Config, args) -> int:
    store = DeviceStore(cfg.data_dir, readonly=True)
    sid = _session(store, args.session)
    seen = set()
    lines = []
    if args.source in ("cloud", "all") and cfg.cloud_url:
        try:
            for line in _client(cfg).stream("/results?session=" + urllib.parse.quote(sid), 120):
                rec = json.loads(line)
                seen.add((rec["window"], rec["mode"]))
                lines.append(((rec["window"], rec["mode"]), line))
        except OSError as e:
            if args.source == "cloud":
                raise SystemExit(f"cloud unreachable: {e}")
            print(f"warning: cloud unreachable ({e}); showing device results only",
                  file=sys.stderr)
    if args.source in ("device", "all"):
        for w, mode, body in store.conn.execute(
                "SELECT window, mode, body FROM results WHERE session_id=?", (sid,)):
            if (w, mode) not in seen:
                lines.append(((w, mode), result_line(sid, json.loads(body))))
    for _key, line in sorted(lines, key=lambda x: x[0]):
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
    return 0


def cmd_sessions(cfg: Config, _args) -> int:
    store = DeviceStore(cfg.data_dir, readonly=True)
    for sid, seq, e1, e2, dev_next, cloud_done in store.sessions():
        print(json.dumps({"session": sid, "seq": seq, "end_1": e1, "end_2": e2,
                          "offline_next_window": dev_next, "cloud_done_window": cloud_done}))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="Never-lose-a-heartbeat pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the pipeline service")
    sub.add_parser("status", help="one JSON line: mode, samples saved/in cloud, backlog")
    e = sub.add_parser("export", help="samples then losses, as JSON lines")
    e.add_argument("--session", required=True, help="session id or 'latest'")
    e.add_argument("--from", dest="source", choices=["device", "cloud"], default="device")
    r = sub.add_parser("results", help="one JSON line per window and mode")
    r.add_argument("--session", required=True, help="session id or 'latest'")
    r.add_argument("--from", dest="source", choices=["device", "cloud", "all"], default="all")
    sub.add_parser("sessions", help="list recorded sessions")
    args = p.parse_args(argv)
    cfg = Config.load()
    handler = {"run": cmd_run, "status": cmd_status, "export": cmd_export,
               "results": cmd_results, "sessions": cmd_sessions}[args.cmd]
    try:
        return handler(cfg, args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
