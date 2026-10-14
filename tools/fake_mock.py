"""Development stand-in for biosignal-mock (brief §6). NOT the official mock: final tests and
evidence must use the real one. Implements the documented protocol, modes and faults.

    python tools/fake_mock.py --mode ramp --seed 7 --duration 120 --port 7100 \
        --corrupt 0.02 --dup 0.02 --stall 0.01 --dropout 2:20000:20399 --restart-at 60
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import socket
import sys
import threading
import time
import zlib

SR = 400
BUFFER = 10 * SR
MOD = 8_388_608
CHANNELS = {1: 3, 2: 2}


# ----------------------------------------------------------------------------- signals
def ramp_values(sensor: int, i: int) -> list:
    k = 3 if sensor == 1 else 2
    return [(k * i + c) % MOD for c in range(1, CHANNELS[sensor] + 1)]


class Signal:
    """Synthetic ECG: 60 bpm for minutes 0-2, 90 for 2-4, 72 for 4-6, repeating."""

    def __init__(self, seed: int, total: int):
        self.seed = seed
        beats, t = [], 0.3 * SR
        while t < total + SR:
            beats.append(int(round(t)))
            minute = t / SR / 60
            hr = (60, 90, 72)[int(minute // 2) % 3]
            t += SR * 60 / hr
        self.beats = beats

    def _noise(self, i: int, salt: int) -> int:
        h = (i * 2654435761 + self.seed * 40503 + salt * 97) & 0xFFFFFFFF
        h ^= h >> 15
        h = (h * 2246822519) & 0xFFFFFFFF
        return (h % 2001) - 1000

    def ecg(self, i: int) -> int:
        k = bisect.bisect_right(self.beats, i)
        v = 100_000.0
        for b in self.beats[max(0, k - 2):k + 1]:
            d = i - b
            v += 1_500_000 * math.exp(-(d / 5.0) ** 2)
            v += 300_000 * math.exp(-((d - 100) / 16.0) ** 2)
        return int(v) + 20 * self._noise(i, 1)

    def values(self, sensor: int, i: int) -> list:
        if sensor == 1:
            c2 = self.ecg(i)
            return [int(round(c2 * 0.6)), c2, int(round(c2 * 1.3))]
        return [400_000 + int(5000 * math.sin(2 * math.pi * i / (SR * 20))) + self._noise(i, 2),
                150_000 + int(3000 * math.sin(2 * math.pi * i / (SR * 33))) + self._noise(i, 3)]


def make_truth(mode: str, seed: int, total: int):
    if mode == "ramp":
        return ramp_values
    return Signal(seed, total).values


# ----------------------------------------------------------------------------- server
class Session:
    def __init__(self, args, rng):
        self.id = "%08x" % rng.getrandbits(32)
        self.t0 = time.monotonic()
        self.total = args.duration * SR
        self.ptr = {1: 0, 2: 0}
        self.values = make_truth(args.mode, args.seed, self.total)
        self.dropouts = {1: [], 2: []}
        for spec in args.dropout:
            s, a, b = (int(x) for x in spec.split(":"))
            self.dropouts[s].append((a, b))

    def produced(self) -> int:
        return min(self.total, int((time.monotonic() - self.t0) * SR))

    def read(self, sensor: int, from_index):
        p = self.produced()
        oldest = max(0, p - BUFFER)
        start = self.ptr[sensor] if from_index is None else max(0, int(from_index))
        lost = []
        if start < oldest:
            lost.append({"from_index": start, "to_index": oldest - 1, "reason": "buffer_overflow"})
            start = oldest
        for a, b in self.dropouts[sensor]:
            if a <= start <= b:
                if p > b:
                    lost.append({"from_index": start, "to_index": b, "reason": "sensor_dropout"})
                    start = b + 1
                else:
                    return start, [], lost, False
        end = max(start, min(p, start + SR))
        for a, _b in self.dropouts[sensor]:
            if start < a < end:
                end = a
        data = [self.values(sensor, i) for i in range(start, end)]
        self.ptr[sensor] = end
        return start, data, lost, (p == self.total and end == self.total)


def reply_line(session_id, sensor, first, data, lost, end, corrupt=False) -> bytes:
    data_text = json.dumps(data)
    crc = zlib.crc32(data_text.encode()) & 0xFFFFFFFF
    if corrupt:
        crc ^= 0x1
    return ('{"session_id": %s, "sensor": %d, "first_index": %d, "count": %d, "lost": %s, '
            '"session_end": %s, "crc32": "%08x", "data": %s}\n' % (
                json.dumps(session_id), sensor, first, len(data), json.dumps(lost),
                "true" if end else "false", crc, data_text)).encode()


def serve(args) -> None:
    rng = random.Random(args.fault_seed)
    state = {"session": Session(args, rng)}
    started = time.monotonic()
    restarted = {"done": False}
    lock = threading.Lock()

    def maybe_restart():
        if args.restart_at and not restarted["done"] and time.monotonic() - started >= args.restart_at:
            restarted["done"] = True
            state["session"] = Session(args, rng)
            print(f"[fake-mock] restarted: new session {state['session'].id}", flush=True)
            return True
        return False

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    print(f"[fake-mock] session {state['session'].id} mode={args.mode} port={args.port}", flush=True)
    while True:
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        try:
            for raw in f:
                with lock:
                    if maybe_restart():
                        break  # the real mock drops clients when it restarts
                    try:
                        req = json.loads(raw)
                        sensor = int(req["sensor"])
                        assert req.get("cmd") == "read" and sensor in CHANNELS
                    except Exception:  # noqa: BLE001
                        conn.sendall(b'{"error": "bad request"}\n')
                        continue
                    sess = state["session"]
                    first, data, lost, end = sess.read(sensor, req.get("from_index"))
                    corrupt = rng.random() < args.corrupt
                    line = reply_line(sess.id, sensor, first, data, lost, end, corrupt)
                if rng.random() < args.stall:
                    time.sleep(args.stall_secs)
                conn.sendall(line)
                if rng.random() < args.dup:
                    conn.sendall(line)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["ramp", "signal"], default="ramp")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--duration", type=int, default=3600)
    p.add_argument("--port", type=int, default=7100)
    p.add_argument("--corrupt", type=float, default=0.0)
    p.add_argument("--dup", type=float, default=0.0)
    p.add_argument("--stall", type=float, default=0.0)
    p.add_argument("--stall-secs", type=float, default=3.0)
    p.add_argument("--dropout", action="append", default=[], help="sensor:from:to")
    p.add_argument("--restart-at", type=float, default=0.0)
    p.add_argument("--fault-seed", type=int, default=1)
    serve(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
