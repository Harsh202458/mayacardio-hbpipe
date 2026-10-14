# Test evidence

## A. Development runs (Windows 11 x86-64 PC, Python 3.14, `tools/fake_mock.py`) — 2026-10-14

These validate the logic before moving to the ARM64 board. They are **not** the submission
evidence: section B must be run on the ARM64 environment with the official mock.

| Test | Result |
|---|---|
| Unit tests (protocol, processing, collector): 26 | **PASS** |
| E2E faults: ramp 150 s; corrupt 2 %, duplicate 2 %, 3 s stalls, sensor-2 dropout 20000–20399; 30 s network cut; 2× `kill -9` | **PASS** — 119,600 samples, 0 wrong, 0 silent holes, only loss = injected dropout; device export == cloud export; 150/150 windows have results; offline 5.2 s after cut, online 1.9 s after restore |
| E2E mock restart at 40 s (+ network cut, 2× kill) | **PASS** — old session tail listed as `{"to_index": null, "reason": "mock_restart"}` per sensor; new session complete; device == cloud |
| E2E ECG signal (same faults) | **PASS** — online latency median 0.92 s, p95 1.39 s, max 3.34 s (limit 5 s); HR 60.0 bpm in the 60-bpm section; online and offline results identical |
| Catch-up of a 2 h backlog (5.76 M samples + 7,200 offline results) to a local cloud | **PASS** — 17 s, 29.3 MB sent (limit 15 min) |

## B. Submission evidence (ARM64 environment, official mock) — TO DO

| # | Test | Script | Date | Env | Result | Logs |
|---|---|---|---|---|---|---|
| T1 | Clean 1 h ramp | — | | | | |
| T2 | 20× kill -9 | `tests/chaos/kill_loop.sh` | | | | |
| T3 | Reboot | `sudo reboot` + `after_power_cut.sh` | | | | |
| T4 | Power cut ×5 | `power_cut.md` | | | | |
| T5 | Network cut 30 s / 10 min / 2 h | `net_cut.sh` | | | | |
| T6 | Network flapping | `net_flap.sh` | | | | |
| T7 | Disk full | `disk_full.sh` | | | | |
| T8 | Clock jumps | `clock_skew.sh` | | | | |
| T9 | Mock faults | official mock fault modes | | | | |
| T10 | Results vs reference (signal) | `pipeline results` | | | | |
| T11 | Combined | net cut + kill + clock | | | | |
| R | 1 h resources | `tools/resource_monitor.py` | | | | |
