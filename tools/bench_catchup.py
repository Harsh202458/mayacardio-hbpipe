"""Catch-up benchmark (criterion 6): build a device backlog equal to an N-hour outage, start a
cloud, and time how long the uploader takes until the cloud has everything.

    python tools/bench_catchup.py --hours 2 [--cloud-url https://host --token T]

Without --cloud-url a local cloud server is started (measures device + server CPU, not WAN).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from hbpipe.common.codec import pack  # noqa: E402
from hbpipe.common.chunkdb import transaction  # noqa: E402
from hbpipe.device.config import Config  # noqa: E402
from hbpipe.device.store import DeviceStore  # noqa: E402
from hbpipe.device.uplink import CloudClient, Uploader  # noqa: E402

SR = 400


def build_backlog(store: DeviceStore, sid: str, hours: float, chunk: int) -> int:
    total = int(hours * 3600 * SR)
    c = store.conn
    with transaction(c):
        c.execute("INSERT INTO sessions(session_id, seq, boot_id) VALUES(?, 1, 'bench')", (sid,))
        for sensor, k in ((1, 3), (2, 2)):
            rows = []
            for first in range(0, total, chunk):
                data = [[(k * i + ch) % 8388608 for ch in range(1, k + 1)]
                        for i in range(first, min(total, first + chunk))]
                rows.append((sid, sensor, first, len(data), pack(data)))
            c.executemany("INSERT INTO chunks(session_id, sensor, first_index, count, data) "
                          "VALUES(?,?,?,?,?)", rows)
            c.execute("INSERT INTO cursors VALUES(?,?,?)", (sid, sensor, total))
        body = json.dumps({"window": 0, "missing_1": 0, "missing_2": 0, "complete": True,
                           "mean_1": [1.0, 2.0, 3.0], "min_1": [1, 2, 3], "max_1": [1, 2, 3],
                           "mean_2": [1.0, 2.0], "min_2": [1, 2], "max_2": [1, 2],
                           "magnitude": 1.0, "heart_rate": 72.0, "mode": "offline"})
        c.executemany("INSERT INTO results(session_id, window, mode, body) VALUES(?,?,?,?)",
                      [(sid, w, "offline", body) for w in range(total // SR)])
    return total


class OnlineStub:
    mode = "online"

    def report(self, ok, source):
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=2.0)
    p.add_argument("--chunk", type=int, default=40, help="samples per stored chunk (poll size)")
    p.add_argument("--cloud-url")
    p.add_argument("--token", default="bench")
    a = p.parse_args(argv)

    work = tempfile.mkdtemp(prefix="hbbench-")
    server = None
    try:
        url = a.cloud_url
        if not url:
            env = dict(os.environ, HB_CLOUD_TOKEN=a.token, PYTHONPATH=ROOT)
            server = subprocess.Popen([sys.executable, "-m", "hbpipe.cloud.server", "--db",
                                       os.path.join(work, "cloud.db"), "--port", "18090"],
                                      cwd=ROOT, env=env, stderr=subprocess.DEVNULL)
            url = "http://127.0.0.1:18090"
            for _ in range(50):
                try:
                    urllib.request.urlopen(url + "/health", timeout=1)
                    break
                except OSError:
                    time.sleep(0.2)
        store = DeviceStore(os.path.join(work, "dev"))
        sid = "bench%04d" % (int(time.time()) % 10000)
        t = time.time()
        total = build_backlog(store, sid, a.hours, a.chunk)
        print(f"backlog: {2 * total:,} samples, {total // SR:,} offline results "
              f"(built in {time.time() - t:.0f}s)")

        cfg = Config(data_dir=store.data_dir, device_id="bench")
        client = CloudClient(url, a.token)
        stop = threading.Event()
        up = Uploader(cfg, client, store, OnlineStub(), stop)
        th = threading.Thread(target=up.run, daemon=True)
        t0 = time.time()
        th.start()
        while True:
            left = store.counts()["backlog"]
            if left == 0:
                break
            time.sleep(1)
            print(f"  {time.time() - t0:6.0f}s  backlog {left:,}", end="\r")
        elapsed = time.time() - t0
        stop.set()
        th.join(5)
        st = json.loads(client.call("GET", "/status?session=" + sid, timeout=30))
        print(f"\ncatch-up done in {elapsed:.0f}s ({elapsed / 60:.1f} min, limit 15 min); "
              f"sent {client.bytes_sent / 1e6:.1f} MB; cloud has {st['samples']:,} samples, "
              f"results {st['results']}")
        ok = st["samples"] == 2 * total and elapsed <= 900
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if server:
            server.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
