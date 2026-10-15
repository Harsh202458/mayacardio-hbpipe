"""End-to-end check against the deployed cloud (Render + Neon) from the ARM64 device container.

    python tests/realcloud_check.py --env-file ~/.hbpipe/device.env --out DIR [--duration 600]

The env file holds HB_CLOUD_URL and HB_CLOUD_TOKEN; it is passed to Docker as a file and never
printed. The run: ECG signal session, a 60 s network cut (device disconnected from the Docker
network; the mock is on loopback and keeps running), then checks that the device and cloud
exports are identical and complete, mode-switch times from the device event log, and online
latency over the internet (cloud processed_at vs the mock's logged session start).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from verify_export import verify  # noqa: E402

DEV = "hb-real"


def sh(*args, check=True, timeout=1800):
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args[:4])}...\n{r.stderr[-500:]}")
    return r.stdout.strip()


def dexec(cmd, check=True, timeout=1800):
    return sh("docker", "exec", DEV, "sh", "-c", cmd, check=check, timeout=timeout)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--env-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=int, default=600)
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    log = []

    def note(msg):
        line = f"[{time.time() - t0:7.1f}s] {msg}"
        print(line, flush=True)
        log.append(line)

    sh("docker", "rm", "-f", DEV, check=False)
    sh("docker", "volume", "rm", "-f", "hb-real-data", check=False)
    t0 = time.time()
    sh("docker", "run", "-d", "--name", DEV, "--platform", "linux/arm64", "--cpus", "4",
       "--memory", "2g", "-v", "hb-real-data:/data", "--env-file", a.env_file,
       "-e", "HB_DEVICE_ID=arm64-realcloud", "-e", "MOCK_MODE=signal",
       "-e", f"MOCK_DURATION={a.duration}", "hbpipe-device")
    note("device started against the deployed cloud")

    def at(sec):
        time.sleep(max(0.0, t0 + sec - time.time()))

    def status():
        for _ in range(20):
            try:
                return json.loads(dexec("pipeline status"))
            except (RuntimeError, ValueError):
                time.sleep(2)
        raise RuntimeError("status unavailable")

    at(150)
    cut_wall = time.time()
    sh("docker", "network", "disconnect", "bridge", DEV)
    note("network CUT (device disconnected)")
    at(210)
    sh("docker", "network", "connect", "bridge", DEV)
    restore_wall = time.time()
    note("network RESTORED")
    at(a.duration + 30)
    for _ in range(300):
        st = status()
        if st["backlog"] == 0 and st["mode"] == "online":
            break
        time.sleep(3)
    note(f"final status {st}")
    time.sleep(15)  # let the cloud worker finish the last windows

    sid = st["session"]
    files = {}
    for src in ("device", "cloud"):
        files[src] = os.path.join(a.out, f"{src}-{sid}.jsonl")
        with open(files[src], "w", encoding="utf-8") as f:
            f.write(dexec(f"pipeline export --session {sid} --from {src}") + "\n")
    res_txt = dexec(f"pipeline results --session {sid} --from cloud")
    with open(os.path.join(a.out, f"results-{sid}.jsonl"), "w", encoding="utf-8") as f:
        f.write(res_txt + "\n")
    events = dexec("cat /data/events.jsonl")
    mock_log = dexec("cat /data/logs/mock.log")
    with open(os.path.join(a.out, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write(events + "\n")

    rep = verify(files["device"], "signal", 7, a.duration * 400, compare=files["cloud"])
    results = [json.loads(l) for l in res_txt.splitlines() if l.strip()]
    wins = {r["window"] for r in results}
    missing_w = [w for w in range(a.duration) if w not in wins]
    mock_t0 = next(float(dict(x.split("=") for x in l.split()[2:])["t0_wall"])
                   for l in mock_log.splitlines() if "session_start" in l)
    lat = sorted(r["processed_at"] - (mock_t0 + r["window"] + 1) for r in results
                 if r["mode"] == "online" and not 140 <= r["window"] <= 260)
    modes = [json.loads(l) for l in events.splitlines() if '"event": "mode"' in l]
    off = next((m["wall"] for m in modes if m["mode"] == "offline" and m["wall"] > cut_wall - 1), None)
    on = next((m["wall"] for m in modes if m["mode"] == "online" and m["wall"] > restore_wall - 1), None)
    hr = {r["window"]: r["heart_rate"] for r in results}
    summary = {
        "test": "Deployed cloud (Render free + Neon free) from the ARM64 device: ECG session, 60 s cut",
        "cloud": "https://hbpipe-cloud.onrender.com",
        "session": sid,
        "export_check": {k: rep[k] for k in ("PASS", "duplicates", "sensor_1", "sensor_2", "compare")},
        "windows_without_result": missing_w[:20],
        "results_by_mode": {m: sum(r["mode"] == m for r in results) for m in ("online", "offline")},
        "offline_after_cut_s": None if off is None else round(off - cut_wall, 2),
        "online_after_restore_s": None if on is None else round(on - restore_wall, 2),
        "online_latency_s": {"n": len(lat), "median": round(lat[len(lat) // 2], 2),
                             "p95": round(lat[int(len(lat) * 0.95)], 2), "max": round(lat[-1], 2)}
        if lat else None,
        "heart_rate_samples": {w: hr.get(w) for w in (60, 150, 300, 450, 550)},
        "log": log,
    }
    summary["PASS"] = bool(rep["PASS"] and not missing_w and summary["offline_after_cut_s"] is not None
                           and summary["offline_after_cut_s"] <= 10
                           and summary["online_after_restore_s"] is not None
                           and summary["online_after_restore_s"] <= 10)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("log", "export_check")}, indent=2))
    print("export check PASS:", rep["PASS"], "| device == cloud:", rep.get("compare"))
    sh("docker", "rm", "-f", DEV, check=False)
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
