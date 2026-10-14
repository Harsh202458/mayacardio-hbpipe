"""End-to-end test on one machine (any OS): fake mock with faults + cloud + cut proxy + pipeline.

    python tests/e2e_local.py                       # ~3 min: faults, network cut, 2x kill -9
    python tests/e2e_local.py --scenario restart    # mock restart mid-session
    python tests/e2e_local.py --mode signal          # realistic ECG, checks heart rate too

Pass criteria: device and cloud exports have every sample exactly once, unchanged, every
missing index recorded with a reason; device == cloud; every window has a result; mode
switches happen within 10 s of the cut and of the restore.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from verify_export import verify  # noqa: E402

PY = sys.executable
TOKEN = "e2e-token"


def spawn(args, log, env=None):
    return subprocess.Popen([PY] + args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env=env)


def pipeline(args, env):
    out = subprocess.run([PY, "-m", "hbpipe.device.cli"] + args, cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError(out.stderr)
    return out.stdout


def wait_http(url, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} not up")


def events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=["faults", "restart"], default="faults")
    p.add_argument("--mode", choices=["ramp", "signal"], default="ramp")
    p.add_argument("--duration", type=int, default=150)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--cloud-db", help="postgres:// URL for the cloud (default: SQLite in work dir)")
    a = p.parse_args(argv)

    work = tempfile.mkdtemp(prefix="hbe2e-")
    print("work dir:", work)
    ports = {"mock": 17100, "cloud": 18080, "proxy": 18081}
    flag = os.path.join(work, "netcut")
    logs = {k: open(os.path.join(work, f"{k}.log"), "ab") for k in ("mock", "cloud", "proxy", "pipeline")}
    env = dict(os.environ, HB_CONFIG=os.path.join(work, "none.env"), HB_DATA_DIR=os.path.join(work, "dev"),
               HB_MOCK_PORT=str(ports["mock"]), HB_CLOUD_URL=f"http://127.0.0.1:{ports['proxy']}",
               HB_CLOUD_TOKEN=TOKEN, HB_DEVICE_ID="e2e", PYTHONPATH=ROOT)
    cloud_env = dict(os.environ, HB_CLOUD_TOKEN=TOKEN, PYTHONPATH=ROOT)

    mock_args = ["tools/fake_mock.py", "--mode", a.mode, "--duration", str(a.duration),
                 "--port", str(ports["mock"])]
    if a.scenario == "faults":
        mock_args += ["--corrupt", "0.02", "--dup", "0.02", "--stall", "0.003", "--stall-secs", "3",
                      "--dropout", "2:20000:20399"]
    else:
        mock_args += ["--restart-at", str(a.duration // 3)]

    procs = {}
    try:
        procs["cloud"] = spawn(["-m", "hbpipe.cloud.server", "--db",
                                a.cloud_db or os.path.join(work, "cloud", "cloud.db"),
                                "--port", str(ports["cloud"])], logs["cloud"], cloud_env)
        procs["proxy"] = spawn(["tools/cut_proxy.py", "--listen", str(ports["proxy"]),
                                "--target", f"127.0.0.1:{ports['cloud']}", "--flag", flag], logs["proxy"])
        wait_http(f"http://127.0.0.1:{ports['cloud']}/health")
        procs["mock"] = spawn(mock_args, logs["mock"])
        t0 = time.time()
        time.sleep(0.5)
        procs["pipeline"] = spawn(["-m", "hbpipe.device.cli", "run"], logs["pipeline"], env)

        def at(sec):
            time.sleep(max(0.0, t0 + sec - time.time()))

        def kill_and_restart(pause):
            procs["pipeline"].kill()  # SIGKILL / TerminateProcess: no clean shutdown
            procs["pipeline"].wait()
            time.sleep(pause)
            procs["pipeline"] = spawn(["-m", "hbpipe.device.cli", "run"], logs["pipeline"], env)

        d = a.duration
        cut_start, cut_end = 0.15 * d, 0.15 * d + 30
        at(cut_start)
        open(flag, "w").close()
        print(f"[{time.time() - t0:5.1f}s] network CUT")
        at(cut_end)
        os.remove(flag)
        print(f"[{time.time() - t0:5.1f}s] network RESTORED")
        at(0.5 * d)
        kill_and_restart(1.0)
        print(f"[{time.time() - t0:5.1f}s] pipeline KILLED, restarted after 1 s")
        at(0.7 * d)
        kill_and_restart(3.0)
        print(f"[{time.time() - t0:5.1f}s] pipeline KILLED, restarted after 3 s")
        # After a restart the new session runs for the full duration again.
        at(d + (d // 3 if a.scenario == "restart" else 0) + 3)

        # Catch-up: wait for an empty backlog.
        deadline = time.time() + 120
        while time.time() < deadline:
            st = json.loads(pipeline(["status"], env))
            if st["backlog"] == 0 and st["mode"] == "online":
                break
            time.sleep(1)
        print("status:", json.dumps(st))
        time.sleep(3)  # let the cloud worker finish the last windows

        ok = True
        sessions = [json.loads(l)["session"] for l in pipeline(["sessions"], env).splitlines()]
        total = d * 400
        for i, sid in enumerate(sessions):
            dev = os.path.join(work, f"device-{sid}.jsonl")
            cld = os.path.join(work, f"cloud-{sid}.jsonl")
            with open(dev, "w") as f:
                f.write(pipeline(["export", "--session", sid, "--from", "device"], env))
            with open(cld, "w") as f:
                f.write(pipeline(["export", "--session", sid, "--from", "cloud"], env))
            last = i == len(sessions) - 1
            rep = verify(dev, a.mode, 7, total if last else None, compare=cld)
            print(f"\n== session {sid}: PASS={rep['PASS']}")
            for s in (1, 2):
                r = rep[f"sensor_{s}"]
                print(f"   sensor {s}: samples={r['samples']} wrong={r['wrong_values']} "
                      f"lost={r['lost_samples']} holes={r['silent_holes']} losses={r['loss_records']}")
            print("   device == cloud:", rep.get("compare"))
            ok &= rep["PASS"]

            # Results: every window has >= 1 result; online and offline agree where both exist.
            res = [json.loads(l) for l in pipeline(["results", "--session", sid], env).splitlines()]
            by_w = {}
            for r in res:
                by_w.setdefault(r["window"], {})[r["mode"]] = r
            n_windows = max(by_w) + 1 if by_w else 0
            missing_w = [w for w in range(n_windows) if w not in by_w]
            both = [w for w, m in by_w.items() if len(m) == 2]
            mismatch = []
            for w in both:
                x, y = dict(by_w[w]["online"]), dict(by_w[w]["offline"])
                for k in ("mode", "processed_at"):
                    x.pop(k, None)
                    y.pop(k, None)
                if x != y:
                    mismatch.append(w)
            modes = {m: sum(1 for r in res if r["mode"] == m) for m in ("online", "offline")}
            print(f"   results: windows={n_windows} {modes} missing={missing_w[:10]} "
                  f"both={len(both)} online!=offline={mismatch[:10]}")
            ok &= not missing_w and not mismatch
            if last and a.scenario == "faults":
                # Criterion 4: online result within 5 s of its second ending. Window w ends
                # at mock start + (w+1) s; t0 is slightly before the mock start, so this is
                # conservative. Windows during the cut + 15 s of catch-up are excluded.
                lat = sorted(r["processed_at"] - (t0 + w + 1) for w, m in by_w.items()
                             for r in [m.get("online")] if r
                             and not (cut_start - 2 <= w + 1 <= cut_end + 15)
                             and not (0.5 * d - 1 <= w + 1 <= 0.5 * d + 6)
                             and not (0.7 * d - 1 <= w + 1 <= 0.7 * d + 8))
                if lat:
                    print(f"   online latency: n={len(lat)} median={lat[len(lat) // 2]:.2f}s "
                          f"p95={lat[int(len(lat) * .95)]:.2f}s max={lat[-1]:.2f}s")
                    ok &= lat[-1] <= 5.0
            if a.mode == "signal":
                hrs = [by_w[w].get("online", by_w[w].get("offline"))["heart_rate"] for w in (60, 100)
                       if w in by_w]
                print("   heart rate at w=60,100:", hrs)

        # Mode switch timing from the event log.
        ev = events(os.path.join(work, "dev", "events.jsonl"))
        modes = [(e["wall"] - t0, e["mode"]) for e in ev if e["event"] == "mode"]
        print("\nmode events (s since start):", [(round(t, 1), m) for t, m in modes])
        off = [t for t, m in modes if m == "offline" and t >= cut_start]
        on = [t for t, m in modes if m == "online" and t >= cut_end]
        off_delay = off[0] - cut_start if off else None
        on_delay = on[0] - cut_end if on else None
        print(f"offline after cut: {off_delay and round(off_delay, 1)} s; "
              f"online after restore: {on_delay and round(on_delay, 1)} s")
        ok &= off_delay is not None and off_delay <= 10 and on_delay is not None and on_delay <= 10

        print("\nOVERALL:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        for pr in procs.values():
            pr.kill()
        for lg in logs.values():
            lg.close()
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
