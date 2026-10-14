"""Check a `pipeline export` against the known signal (our stand-in for check_ground_truth).

    python tools/verify_export.py device.jsonl --mode ramp --seed 7 [--total 48000]
                                  [--compare cloud.jsonl]

Fails (exit 1) on: duplicate samples, changed values, losses overlapping samples or each
other, and silent loss (an index in [0, total) that is neither a sample nor a recorded loss).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mock import make_truth  # noqa: E402


def load(path):
    samples = {1: {}, 2: {}}
    losses = {1: [], 2: []}
    dups = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            s = rec["sensor"]
            if "index" in rec:
                if rec["index"] in samples[s]:
                    dups += 1
                samples[s][rec["index"]] = rec["values"]
            else:
                losses[s].append((rec["from_index"], rec["to_index"], rec["reason"]))
    return samples, losses, dups


def verify(path, mode, seed, total=None, compare=None) -> dict:
    samples, losses, dups = load(path)
    if total is None:
        ends = [max(samples[s]) + 1 for s in (1, 2) if samples[s]]
        ends += [to + 1 for s in (1, 2) for _f, to, _r in losses[s] if to is not None]
        total = max(ends or [0])
    truth = make_truth(mode, seed, total)
    report = {"file": path, "total_per_sensor": total, "duplicates": dups}
    ok = dups == 0
    for s in (1, 2):
        wrong = [i for i, v in samples[s].items() if v != truth(s, i)]
        covered = bytearray(total)
        overlap = 0
        lost_n = 0
        for frm, to, _r in losses[s]:
            hi = total - 1 if to is None else min(to, total - 1)
            for i in range(frm, hi + 1):
                if covered[i] or i in samples[s]:
                    overlap += 1
                covered[i] = 1
                lost_n += 1
        for i in samples[s]:
            if i < total:
                covered[i] = 1
        holes = []
        i = 0
        while i < total:
            if not covered[i]:
                j = i
                while j + 1 < total and not covered[j + 1]:
                    j += 1
                holes.append([i, j])
                i = j + 1
            else:
                i += 1
        report[f"sensor_{s}"] = {
            "samples": len(samples[s]), "wrong_values": len(wrong), "first_wrong": wrong[:5],
            "lost_samples": lost_n, "loss_records": [list(l) for l in losses[s]][:20],
            "loss_sample_overlaps": overlap, "silent_holes": holes[:20]}
        ok = ok and not wrong and not overlap and not holes
    if compare:
        s2, l2, _ = load(compare)
        same = all(samples[s] == s2[s] for s in (1, 2))
        report["compare"] = {"file": compare, "identical_samples": same,
                             "identical_losses": all(sorted(losses[s]) == sorted(l2[s]) for s in (1, 2))}
        ok = ok and same and report["compare"]["identical_losses"]
    report["PASS"] = ok
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("export")
    p.add_argument("--mode", choices=["ramp", "signal"], default="ramp")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--total", type=int)
    p.add_argument("--compare")
    a = p.parse_args(argv)
    rep = verify(a.export, a.mode, a.seed, a.total, a.compare)
    print(json.dumps(rep, indent=2))
    return 0 if rep["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
