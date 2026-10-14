"""Resource figures for criterion 8 (Linux): average CPU, peak memory, disk MB written and
network MB sent, for the pipeline process tree over a measurement period.

    sudo python3 tools/resource_monitor.py --pid $(systemctl show -p MainPID --value hbpipe) \
        --iface eth0 --seconds 3600 --out evidence/resources.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def tree(pid: int) -> list:
    pids = [pid]
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                pids += [int(x) for x in f.read().split()]
    except OSError:
        pass
    return pids


def cpu_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])  # utime + stime


def status_kb(pid: int, key: str) -> int:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1])
    return 0


def write_bytes(pid: int) -> int:
    with open(f"/proc/{pid}/io") as f:
        for line in f:
            if line.startswith("write_bytes:"):
                return int(line.split()[1])
    return 0


def net_tx(iface: str) -> int:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                return int(line.split(":", 1)[1].split()[8])
    raise SystemExit(f"interface {iface} not found")


def sample(pid):
    pids = tree(pid)
    return (sum(cpu_ticks(p) for p in pids), sum(write_bytes(p) for p in pids),
            sum(status_kb(p, "VmRSS") for p in pids), sum(status_kb(p, "VmHWM") for p in pids))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--iface", required=True)
    p.add_argument("--seconds", type=int, default=3600)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--out")
    a = p.parse_args(argv)
    t0 = time.monotonic()
    cpu0, wr0, _rss, _hwm = sample(a.pid)
    tx0 = net_tx(a.iface)
    peak_rss = 0
    peak_hwm = 0
    while time.monotonic() - t0 < a.seconds:
        time.sleep(a.interval)
        _c, _w, rss, hwm = sample(a.pid)
        peak_rss = max(peak_rss, rss)
        peak_hwm = max(peak_hwm, hwm)
    cpu1, wr1, _rss, _hwm = sample(a.pid)
    tx1 = net_tx(a.iface)
    elapsed = time.monotonic() - t0
    cpu_s = (cpu1 - cpu0) / TICK
    rep = {
        "environment": {"machine": platform.machine(), "kernel": platform.release(),
                        "cpus": os.cpu_count(), "node": platform.node()},
        "seconds": round(elapsed),
        "avg_cpu_percent_of_one_core": round(100 * cpu_s / elapsed, 2),
        "avg_cpu_percent_of_all_cores": round(100 * cpu_s / elapsed / (os.cpu_count() or 1), 2),
        "peak_memory_mb": round(max(peak_rss, peak_hwm) / 1024, 1),
        "disk_written_mb": round((wr1 - wr0) / 1e6, 2),
        "network_sent_mb": round((tx1 - tx0) / 1e6, 2),
    }
    print(json.dumps(rep, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
