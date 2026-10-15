"""Resource figures for criterion 8 (Linux): average CPU, peak memory, disk MB written and
network MB sent over a measurement period.

    python3 tools/resource_monitor.py --pid <pipeline pid> --iface eth0 --seconds 3600 --out r.json

Two views are reported when available:
  * process: the pipeline process tree, from /proc/<pid>/{stat,status,io};
  * cgroup:  the whole service/container cgroup (cgroup v2: cpu.stat, memory.peak, io.stat).
    On a Pi run inside `systemd-run`/the hbpipe.service cgroup; in Docker it is the container
    (pipeline + mock), which is conservative.
Network bytes are read from /proc/net/dev for the given interface (uploads to the cloud; the
mock is on loopback).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
CG = "/sys/fs/cgroup"


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


def write_bytes(pid: int):
    try:
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except OSError:
        return None  # kernel without per-task I/O accounting
    return None


def net_tx(iface: str) -> int:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                return int(line.split(":", 1)[1].split()[8])
    raise SystemExit(f"interface {iface} not found")


def read_kv(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(" ")
                out[k] = v.strip()
    except OSError:
        pass
    return out


def cg_cpu_usec():
    v = read_kv(f"{CG}/cpu.stat").get("usage_usec")
    return int(v) if v else None


def cg_wbytes():
    try:
        with open(f"{CG}/io.stat") as f:
            total = 0
            for line in f:
                for field in line.split()[1:]:
                    if field.startswith("wbytes="):
                        total += int(field[7:])
            return total
    except OSError:
        return None


def cg_mem_peak():
    for name in ("memory.peak", "memory.max_usage_in_bytes"):
        try:
            with open(f"{CG}/{name}") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def cg_mem_now():
    try:
        with open(f"{CG}/memory.current") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def proc_sample(pid):
    pids = tree(pid)
    wb = [write_bytes(p) for p in pids]
    return (sum(cpu_ticks(p) for p in pids),
            None if any(w is None for w in wb) else sum(wb),
            sum(status_kb(p, "VmRSS") for p in pids),
            sum(status_kb(p, "VmHWM") for p in pids))


def mb(x):
    return None if x is None else round(x / 1e6, 2)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--iface", required=True)
    p.add_argument("--seconds", type=int, default=3600)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--out")
    a = p.parse_args(argv)
    ncpu = os.cpu_count() or 1

    t0 = time.monotonic()
    cpu0, wr0, _r, _h = proc_sample(a.pid)
    cg_cpu0, cg_wr0 = cg_cpu_usec(), cg_wbytes()
    tx0 = net_tx(a.iface)
    peak_rss_kb = 0
    peak_cg_mem = 0
    while time.monotonic() - t0 < a.seconds:
        time.sleep(a.interval)
        _c, _w, rss, hwm = proc_sample(a.pid)
        peak_rss_kb = max(peak_rss_kb, rss, hwm)
        peak_cg_mem = max(peak_cg_mem, cg_mem_now() or 0)
    cpu1, wr1, _r, _h = proc_sample(a.pid)
    cg_cpu1, cg_wr1 = cg_cpu_usec(), cg_wbytes()
    tx1 = net_tx(a.iface)
    elapsed = time.monotonic() - t0

    cpu_s = (cpu1 - cpu0) / TICK
    rep = {
        "environment": {"machine": platform.machine(), "kernel": platform.release(),
                        "cpus_visible": ncpu, "node": platform.node()},
        "seconds": round(elapsed),
        "process": {
            "avg_cpu_percent_of_one_core": round(100 * cpu_s / elapsed, 2),
            "avg_cpu_percent_of_all_cores": round(100 * cpu_s / elapsed / ncpu, 2),
            "peak_memory_mb": round(peak_rss_kb * 1024 / 1e6, 1),
            "disk_written_mb": None if wr0 is None or wr1 is None else mb(wr1 - wr0),
        },
        "network_sent_mb": mb(tx1 - tx0),
    }
    if cg_cpu0 is not None and cg_cpu1 is not None:
        cg_s = (cg_cpu1 - cg_cpu0) / 1e6
        rep["cgroup"] = {
            "scope": "whole cgroup (in Docker: pipeline + mock + supervisor)",
            "avg_cpu_percent_of_one_core": round(100 * cg_s / elapsed, 2),
            "avg_cpu_percent_of_all_cores": round(100 * cg_s / elapsed / ncpu, 2),
            "peak_memory_mb": mb(max(peak_cg_mem, cg_mem_peak() or 0)),
            "disk_written_mb": None if cg_wr0 is None or cg_wr1 is None else mb(cg_wr1 - cg_wr0),
        }
    print(json.dumps(rep, indent=2))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rep, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
