# Failure-test evidence

## Environment
- **Device:** `linux/arm64` Debian 12 container (same base as Raspberry Pi OS Bookworm), Docker Desktop, `--cpus 4 --memory 2g`, ARM emulated on an x86-64 Windows PC.
  - The mock and pipeline both run inside it, with a supervisor restarting the pipeline after 0.2 s (systemd's job on a Pi).
- **Cloud for the failure tests:** the same cloud code in a local container, with Postgres.
- **Cloud for the 1-hour resource run:** the deployed cloud (Render free + Neon free).
- **Mock:** `tools/fake_mock.py`, built from brief §6, since the official mock link was blank.
  - The ramp-mode values are checked sample by sample by `tools/verify_export.py`.
  - Each test also compares the device export with the cloud export.
- **Raw evidence:** `3_evidence/arm64/<test>/summary.json`, plus `events.jsonl` (the device's own timestamps), `logs/`, and per-window results. Raw exports are omitted for size; every test re-checks them before writing its summary.

**Pass criteria for every test:**
- every sample exactly once and unchanged;
- every missing index listed with a reason (no silent holes);
- device export == cloud export;
- every window has a result.

## Results (final code)

| # | Test | Result | Key figures |
|---|---|---|---|
| T2 + T9 | **6× `kill -9`** every 45 s, with mock faults (2 % corrupt replies, 2 % duplicate replies, 3 s stalls, sensor-2 dropout) | **PASS** | 330 windows, all with results; device == cloud; no silent holes. The only losses are the injected dropout (400 samples) and 2 slow emulated restarts, recorded as `buffer_overflow` (437 / 540 samples). A run of the same code on 2026-10-14 lost **0** samples. |
| T5 | **Network cut 30 s**, iptables DROP | **PASS** | offline after **4.8 s**, online after **0.3 s** (limit 10 s); backlog drained |
| T5 | **Network cut 3 min** | **PASS** | offline after **3.8 s**, online after **0.9 s**; backlog drained in 60 s |
| T4 | **2 power cuts** (whole device including mock killed with `docker kill`) | **PASS** | measured loss **0.02–0.055 s** per sensor (stated bound **0.5 s**), each listed as `to_index: null`, reason `device_power_loss_or_reboot`; the next session is complete (80,000/80,000) |
| T7 | **Disk full** on a 64 MB filesystem: 2 MB left at 60 s, 0.5 MB at 100 s, freed at 160 s | **PASS** | stage 1 (reserve released) **kept saving**; stage 2 recorded `disk_full` losses only (~59 s); recovered automatically; 240/240 windows |
| T8 | **Clock jumps**: +1 h, −2 h, +1 day during the run | **PASS** | 96,000/96,000 samples per sensor, **0 lost**; device == cloud; 240/240 windows (ordering by sample index, timers monotonic) |
| T5b | **2-hour outage catch-up**: 5.76 M samples + 7,200 offline results uploaded from the ARM64 device | **PASS** | all 5,760,000 samples + 7,200 results in the cloud after **2.9 min** (limit 15 min); 30 MB sent |
| T10 | **Results**: ECG signal, online vs offline | PASS | heart rate 60 / 90 / 72 bpm exactly as scheduled; online and offline results byte-identical; 31 unit tests incl. a full-history peak cross-check |
| R | **1 h online against the deployed cloud** | see below | |

## Resource figures: 1 hour, online mode, ARM64 device → Render + Neon
Measured **2026-10-15 23:02:58 → 2026-10-16 00:03:28** (3,605 s) with `tools/resource_monitor.py`, sampling every 10 s.
- **Device:** linux/arm64 Debian 12 container, `--cpus 4 --memory 2g`, QEMU-emulated on an x86-64 Windows PC.
- **Cloud:** https://hbpipe-cloud.onrender.com (Render free, Ohio) with Neon Postgres (free, us-east-2).
- **Signal:** ECG mock, online mode for the whole hour.

| Figure | Pipeline process | Whole device container (pipeline + mock + supervisor) |
|---|---|---|
| Average CPU (share of all 4 cores) | **5.7 %** (22.6 % of one core) | 15.8 % (63.4 % of one core) |
| Peak memory | **83 MB** | 252 MB |
| Disk written | n/a (no per-task I/O accounting in this kernel) | **263 MB** |
| Network sent (interface) | **27.3 MB** | |

- **Data:** 2,918,751 samples saved; all but the last ~0.4 s were in the cloud at the end (backlog 607). The mode stayed **online** for the whole hour (the event log shows no switch).
- **Online latency, device in India to the Render cloud in Ohio:**
  - cloud `processed_at` vs the end of each second on the mock's clock;
  - 3,696 windows: median **1.13 s**, p95 4.32 s, **96 % within 5 s**.
- **Disturbances:** the slower windows (up to 15 s), plus **13 short `buffer_overflow` gaps (2,478 samples, all listed, none silent)**, happened only in short bursts:
  - at 23:20, 23:30, 23:35–23:42, 23:50 and 00:00;
  - each time while a **second emulated ARM device** was started, network-cut, killed or power-cut on the same PC to record the demo video, so the two competed for the emulated CPU;
  - plus the first 10 s of startup.

  A real Raspberry Pi runs one device natively and does not see this contention.
- **Files:** `4_resources/resources-1h.json`, `status-start.json`, `status-end.json`, `events.jsonl`.

## Supporting measurements
- **Native restart** (x86, no emulation): process start → reading the mock in **0.13–0.36 s**.
  - Under ARM emulation the same takes 3–12 s, hence the rare `buffer_overflow` after a kill. This is far inside the 10 s buffer on a real Pi.
- **Online latency** (local end-to-end): median 1.1 s, worst 4.2 s (limit 5 s).
- **Local end-to-end** (`tests/e2e_local.py`): faults, network cut, 2× kill, mock restart. All PASS; device == cloud.
- **Disk writes:** 1 KB pages, upload progress stored as id ranges, and fsync only for sample commits. This cut writes from ~670 to ~250 MB/h, measured before the final 1-hour run.
