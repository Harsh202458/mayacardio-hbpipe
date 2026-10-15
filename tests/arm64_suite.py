"""Failure-test suite on the ARM64 device container (Docker, linux/arm64, --cpus 4 --memory 2g)
with the cloud container + Postgres on a Docker network. Run on the host:

    python tests/arm64_suite.py smoke kill net power disk clock signal catchup [--out DIR]
    python tests/arm64_suite.py resources            # 1 h online run with resource figures

Each test writes evidence (summary.json, exports, events, logs) to OUT/<test>/.
Requires images: hbpipe-device (docker/Dockerfile.device), hbpipe-cloud (docker/Dockerfile.cloud).
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

TOKEN = "suite-token"
NET = "hbnet"
DEV, CLOUD, PG = "hb-dev", "hb-cloud", "hb-pg"


def sh(*args, check=True, capture=True, timeout=600):
    r = subprocess.run(list(args), capture_output=capture, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip() if capture else ""


def dexec(cmd, check=True, timeout=600):
    return sh("docker", "exec", DEV, "sh", "-c", cmd, check=check, timeout=timeout)


class Env:
    def __init__(self, name, out, mode="ramp", duration=300, mock_args="", tmpfs_mb=0):
        self.name, self.mode, self.duration = name, mode, duration
        self.dir = os.path.join(out, name)
        os.makedirs(self.dir, exist_ok=True)
        self.log_lines = []
        for c in (DEV, CLOUD, PG):
            sh("docker", "rm", "-f", c, check=False)
        sh("docker", "volume", "rm", "-f", f"hb-data-{name}", check=False)
        sh("docker", "network", "create", NET, check=False)
        sh("docker", "run", "-d", "--name", PG, "--network", NET, "-e", "POSTGRES_PASSWORD=pw",
           "-e", "POSTGRES_DB=hb", "postgres:16-alpine")
        for _ in range(60):
            if sh("docker", "exec", PG, "pg_isready", "-U", "postgres", "-d", "hb", check=False).endswith(
                    "accepting connections"):
                break
            time.sleep(1)
        time.sleep(2)
        sh("docker", "run", "-d", "--name", CLOUD, "--network", NET,
           "-e", f"HB_CLOUD_DB=postgresql://postgres:pw@{PG}:5432/hb", "-e", f"HB_CLOUD_TOKEN={TOKEN}",
           "hbpipe-cloud")
        time.sleep(3)
        data = (["--tmpfs", f"/data:size={tmpfs_mb}m"] if tmpfs_mb
                else ["-v", f"hb-data-{name}:/data"])
        sh("docker", "run", "-d", "--name", DEV, "--platform", "linux/arm64", "--network", NET,
           "--cpus", "4", "--memory", "2g", "--cap-add", "NET_ADMIN", "--cap-add", "SYS_TIME",
           *data,
           "-e", f"HB_CLOUD_URL=http://{CLOUD}:8080", "-e", f"HB_CLOUD_TOKEN={TOKEN}",
           "-e", "HB_DEVICE_ID=arm64-test", "-e", f"MOCK_MODE={mode}",
           "-e", f"MOCK_DURATION={duration}", "-e", f"MOCK_ARGS={mock_args}", "hbpipe-device")
        self.t0 = time.time()
        self.log(f"started: mode={mode} duration={duration}s mock_args='{mock_args}'")
        self.cloud_ip = None

    def log(self, msg):
        line = f"[{time.time() - self.t0 if hasattr(self, 't0') else 0:7.1f}s] {msg}"
        print(f"  {self.name}: {line}", flush=True)
        self.log_lines.append(line)

    def at(self, sec):
        time.sleep(max(0.0, self.t0 + sec - time.time()))

    def status(self):
        for _ in range(10):
            try:
                return json.loads(dexec("pipeline status"))
            except (RuntimeError, ValueError):
                time.sleep(1)
        raise RuntimeError("status unavailable")

    def wait_mode(self, mode, limit=60):
        start = time.time()
        while time.time() - start < limit:
            if self.status()["mode"] == mode:
                return round(time.time() - start, 1)
            time.sleep(0.25)
        return None

    def net(self, cut: bool):
        if self.cloud_ip is None:
            self.cloud_ip = dexec(f"getent hosts {CLOUD} | awk '{{print $1}}'")
        op = "-I" if cut else "-D"
        # iptables cannot run under ARM64 emulation; a native helper applies the same DROP rule
        # inside the device's network namespace (packets to the cloud silently vanish).
        sh("docker", "run", "--rm", "--net", f"container:{DEV}", "--cap-add", "NET_ADMIN",
           "hbpipe-netctl", "iptables", op, "OUTPUT", "-d", self.cloud_ip, "-j", "DROP")
        self.log(f"network {'CUT' if cut else 'RESTORED'} ({self.cloud_ip})")

    def kill_pipeline(self):
        dexec("pkill -9 -f 'hbpipe.device.cli run'", check=False)
        self.log("kill -9 pipeline (supervisor restarts it after 1 s)")

    def power_cut(self, off_secs=5):
        sh("docker", "kill", DEV)  # SIGKILL to every process in the device, mock included
        self.log("POWER CUT (docker kill: device and mock die)")
        time.sleep(off_secs)
        sh("docker", "start", DEV)
        self.log("power restored (container started; mock starts a new session)")

    def wait_backlog(self, limit=1200):
        start = time.time()
        while time.time() - start < limit:
            st = self.status()
            if st["backlog"] == 0 and st["mode"] == "online":
                return round(time.time() - start, 1)
            time.sleep(2)
        return None

    def finish(self, summary: dict, expect_total_last=True):
        drained = self.wait_backlog()
        self.log(f"backlog drained after {drained} s")
        time.sleep(5)
        summary["backlog_drain_s"] = drained
        summary["final_status"] = self.status()
        sessions = [json.loads(l)["session"] for l in dexec("pipeline sessions").splitlines()]
        ok = drained is not None
        summary["sessions"] = {}
        for i, sid in enumerate(sessions):
            files = {}
            for src in ("device", "cloud"):
                path = os.path.join(self.dir, f"{src}-{sid}.jsonl")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(dexec(f"pipeline export --session {sid} --from {src}", timeout=1800) + "\n")
                files[src] = path
            res_path = os.path.join(self.dir, f"results-{sid}.jsonl")
            res_txt = dexec(f"pipeline results --session {sid}", timeout=600)
            with open(res_path, "w", encoding="utf-8") as f:
                f.write(res_txt + "\n")
            last = i == len(sessions) - 1
            total = self.duration * 400 if (last and expect_total_last) else None
            rep = verify(files["device"], self.mode, 7, total, compare=files["cloud"])
            results = [json.loads(l) for l in res_txt.splitlines() if l.strip()]
            wins = {r["window"] for r in results}
            n_w = max(wins) + 1 if wins else 0
            missing_w = [w for w in range(n_w) if w not in wins]
            s = {"PASS": rep["PASS"] and not missing_w,
                 "device_equals_cloud": rep.get("compare"),
                 "windows": n_w, "windows_without_result": missing_w[:20],
                 "results_by_mode": {m: sum(r["mode"] == m for r in results) for m in ("online", "offline")}}
            for k in ("sensor_1", "sensor_2"):
                r = rep[k]
                s[k] = {x: r[x] for x in ("samples", "wrong_values", "lost_samples", "loss_records",
                                          "silent_holes", "loss_sample_overlaps")}
            summary["sessions"][sid] = s
            ok = ok and s["PASS"]
        sh("docker", "cp", f"{DEV}:/data/events.jsonl", os.path.join(self.dir, "events.jsonl"), check=False)
        sh("docker", "cp", f"{DEV}:/data/logs", os.path.join(self.dir, "logs"), check=False)
        summary["PASS"] = bool(summary.get("PASS", True) and ok)
        summary["log"] = self.log_lines
        with open(os.path.join(self.dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        self.log(f"RESULT {'PASS' if summary['PASS'] else 'FAIL'}")
        return summary


# ------------------------------------------------------------------------------- tests
def t_smoke(out):
    e = Env("smoke", out, duration=90)
    e.at(95)
    return e.finish({"test": "smoke: 90 s clean ramp run"})


def t_kill(out):
    e = Env("kill", out, duration=330,
            mock_args="--corrupt 0.02 --dup 0.02 --stall 0.003 --stall-secs 3 --dropout 2:40000:40399")
    for k in range(6):
        e.at(40 + 45 * k)
        e.kill_pipeline()
    e.at(335)
    return e.finish({"test": "T2+T9: 6x kill -9 every 45 s, with mock faults (corrupt 2%, dup 2%, "
                             "3 s stalls, sensor-2 dropout)"})


def t_net(out):
    e = Env("net", out, duration=400)
    summary = {"test": "T5: network cut 30 s, then 3 min (iptables DROP)", "switches": []}
    cuts = []
    for start, length in ((60, 30), (150, 180)):
        e.at(start)
        e.net(True)
        cut_wall = time.time()        # rule is in place when the helper returns
        e.at(start + length)
        e.net(False)
        restore_wall = time.time()
        drained = e.wait_backlog(900)
        cuts.append((length, cut_wall, restore_wall, drained))
    e.at(405)
    # Switch delays from the device's own event log (device and host share the Docker VM clock,
    # measured offset ~0.2 s). Polling `pipeline status` through `docker exec` into the emulated
    # container adds seconds of overhead, so it is not used for timing.
    events = dexec("cat /data/events.jsonl")
    summary = e.finish(summary)
    modes = [json.loads(l) for l in events.splitlines() if '"event": "mode"' in l]
    ok = True
    for length, cut_wall, restore_wall, drained in cuts:
        off = next((m["wall"] for m in modes if m["mode"] == "offline" and m["wall"] > cut_wall - 1), None)
        on = next((m["wall"] for m in modes if m["mode"] == "online" and m["wall"] > restore_wall - 1), None)
        sw = {"cut_s": length,
              "offline_after_s": None if off is None else round(off - cut_wall, 2),
              "online_after_s": None if on is None else round(on - restore_wall, 2),
              "backlog_drained_after_s": drained}
        summary["switches"].append(sw)
        e.log(f"cut {length}s: {sw}")
        ok = ok and sw["offline_after_s"] is not None and sw["offline_after_s"] <= 10 \
            and sw["online_after_s"] is not None and sw["online_after_s"] <= 10
    summary["PASS"] = summary["PASS"] and ok
    with open(os.path.join(e.dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def t_power(out):
    e = Env("power", out, duration=200)
    cut_walls = []
    for at in (60, 140):
        e.at(at)
        cut_walls.append(time.time())
        e.power_cut()
    e.at(140 + 5 + 200 + 25)  # last session runs 200 s after an emulated boot of up to ~20 s
    summary = {"test": "T4: 2 power cuts (docker kill of the whole device incl. mock)"}
    summary = e.finish(summary, expect_total_last=True)
    # Measured loss per cut: samples the mock had produced at the cut (from its logged start
    # time) minus the first index of the old session's recorded tail loss.
    starts = {}
    with open(os.path.join(e.dir, "logs", "mock.log"), encoding="utf-8") as f:
        for line in f:
            if "session_start" in line:
                parts = dict(p.split("=") for p in line.split()[2:])
                starts[parts["id"]] = float(parts["t0_wall"])
    tails = []
    for sid, s in summary["sessions"].items():
        for k in ("sensor_1", "sensor_2"):
            for frm, to, reason in s[k]["loss_records"]:
                if to is None and sid in starts:
                    cut = min((c for c in cut_walls if c > starts[sid]), default=None)
                    produced = int((cut - starts[sid]) * 400) if cut else None
                    lost = None if produced is None else produced - frm
                    tails.append({"session": sid, "sensor": k, "tail_from": frm, "reason": reason,
                                  "mock_produced_at_cut": produced, "lost_samples": lost,
                                  "lost_seconds": None if lost is None else round(lost / 400, 3)})
    summary["tail_losses"] = tails
    worst = max((t["lost_seconds"] for t in tails if t["lost_seconds"] is not None), default=None)
    summary["worst_power_cut_loss_s"] = worst
    summary["PASS"] = summary["PASS"] and len(tails) == 4 and worst is not None and worst <= 0.5
    e.log(f"power-cut tail losses: {tails}")
    with open(os.path.join(e.dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def t_disk(out):
    e = Env("disk", out, duration=240, tmpfs_mb=64)

    def fill(name, leave_kb):
        free = int(dexec("df --output=avail -k /data | tail -1"))
        dexec(f"fallocate -l {max(0, free - leave_kb)}k /data/{name}; df -h /data | tail -1")
        e.log(f"disk filled with {name}: {leave_kb} KB left")

    e.at(60)
    fill("filler1", 2048)   # stage 1: reserve released, samples must still be saved
    e.at(100)
    fill("filler2", 512)    # stage 2: no room left, samples recorded as disk_full losses
    e.at(160)
    dexec("rm -f /data/filler1 /data/filler2")
    e.log("space freed")
    e.at(245)
    ev = dexec("grep -E '\"disk_low\"|\"disk_full\"|\"disk_recovered\"' /data/events.jsonl", check=False)
    summary = e.finish({"test": "T7: data dir on 64 MB tmpfs; filled to 2 MB free at 60 s (reserve "
                                "used, saving continues), to 0.5 MB at 100 s (disk_full losses), "
                                "freed at 160 s", "disk_events": ev.splitlines()})
    return summary


def t_clock(out):
    e = Env("clock", out, duration=240)
    host = time.time()
    for at, jump in ((60, "+1 hour"), (110, "-2 hours"), (160, "+1 day")):
        e.at(at)
        dexec(f"date -s '{jump}' >/dev/null")
        e.log(f"clock jumped {jump}: device now {dexec('date -u')}")
    e.at(210)
    dexec(f"date -s @{int(time.time())} >/dev/null")
    e.log(f"clock restored: {dexec('date -u')}")
    e.at(245)
    return e.finish({"test": "T8: system clock +1 h, -2 h, +1 day during the run"})


def t_signal(out):
    e = Env("signal", out, mode="signal", duration=400)
    e.at(150)
    e.net(True)
    e.at(200)
    e.net(False)
    e.at(405)
    summary = e.finish({"test": "T10: ECG signal 400 s with a 50 s network cut: HR, online vs offline, "
                                "online latency"})
    sid = list(summary["sessions"])[-1]
    with open(os.path.join(e.dir, f"results-{sid}.jsonl"), encoding="utf-8") as f:
        res = [json.loads(l) for l in f if l.strip()]
    hr = {r["window"]: r["heart_rate"] for r in res}
    summary["heart_rate_samples"] = {w: hr.get(w) for w in (30, 100, 150, 250, 330)}
    # Latency: processed_at (cloud clock) vs the end of the window on the mock's timeline,
    # anchored at the mock's logged session start (same Docker VM clock).
    mock_t0 = e.t0
    with open(os.path.join(e.dir, "logs", "mock.log"), encoding="utf-8") as f:
        for line in f:
            if "session_start" in line:
                mock_t0 = float(dict(p.split("=") for p in line.split()[2:])["t0_wall"])
    summary["latency_anchor"] = "mock session start" if mock_t0 != e.t0 else "container start"
    lat = sorted(r["processed_at"] - (mock_t0 + r["window"] + 1) for r in res
                 if r["mode"] == "online" and not 140 <= r["window"] <= 230)
    summary["online_latency_s"] = {"n": len(lat), "median": round(lat[len(lat) // 2], 2),
                                   "p95": round(lat[int(len(lat) * 0.95)], 2), "max": round(lat[-1], 2)}
    by_w = {}
    for r in res:
        by_w.setdefault(r["window"], {})[r["mode"]] = r
    diff = [w for w, m in by_w.items() if len(m) == 2 and
            {k: v for k, v in m["online"].items() if k not in ("mode", "processed_at")} !=
            {k: v for k, v in m["offline"].items() if k not in ("mode", "processed_at")}]
    summary["online_vs_offline_mismatches"] = diff
    summary["PASS"] = summary["PASS"] and not diff and summary["online_latency_s"]["max"] <= 5
    with open(os.path.join(e.dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ("heart_rate_samples", "online_latency_s")}))
    return summary


def t_catchup(out):
    e = Env("catchup", out, duration=60)
    txt = dexec(f"cd /opt/hbpipe && python3 tools/bench_catchup.py --hours 2 "
                f"--cloud-url http://{CLOUD}:8080 --token {TOKEN} 2>&1 | tr '\\r' '\\n' | "
                f"grep -v '^  '", timeout=3600)
    e.log(txt.replace("\n", " | "))
    summary = {"test": "T5b: 2 h outage backlog (5.76 M samples + 7200 offline results) uploaded "
                       "from the ARM64 device", "output": txt.splitlines(), "PASS": "PASS" in txt}
    with open(os.path.join(e.dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return summary


def t_resources(out):
    e = Env("resources", out, duration=3700)
    e.at(30)
    pid = dexec("pgrep -f 'hbpipe.device.cli run' | head -1")
    txt = dexec(f"cd /opt/hbpipe && python3 tools/resource_monitor.py --pid {pid} --iface eth0 "
                f"--seconds 3600 --interval 10", timeout=4000)
    rep = json.loads(txt)
    rep["environment"]["note"] = ("Docker Desktop linux/arm64 container (QEMU user-mode emulation on an "
                                  "x86-64 Windows PC), --cpus 4 --memory 2g, Debian 12")
    e.log(json.dumps(rep))
    summary = {"test": "R: 1 h online resource figures", "resources": rep}
    e.at(3705)
    return e.finish(summary)


TESTS = {"smoke": t_smoke, "kill": t_kill, "net": t_net, "power": t_power, "disk": t_disk,
         "clock": t_clock, "signal": t_signal, "catchup": t_catchup, "resources": t_resources}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tests", nargs="+", choices=list(TESTS))
    p.add_argument("--out", default=os.path.join(ROOT, "evidence", "arm64"))
    a = p.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    overall = {}
    for name in a.tests:
        print(f"=== {name}", flush=True)
        try:
            s = TESTS[name](a.out)
            overall[name] = s.get("PASS")
        except Exception as ex:  # noqa: BLE001
            print(f"  {name}: ERROR {ex}", flush=True)
            overall[name] = f"ERROR: {ex}"
    for c in (DEV, CLOUD, PG):
        sh("docker", "rm", "-f", c, check=False)
    print("OVERALL:", json.dumps(overall))
    return 0 if all(v is True for v in overall.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
