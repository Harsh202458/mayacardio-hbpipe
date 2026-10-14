# Complete Build Plan — "Never Lose a Heartbeat" (MayaCardio assessment)

Every requirement in the assessment is listed in **§13 Traceability matrix** with the plan
section that delivers it and the test that proves it. If a row there has no test, the plan
is incomplete.

## Progress (2026-10-14)

| Phase | Status |
|---|---|
| P1 Collect + save | **Built + tested locally** (unit + e2e with faults, kills, mock restart) |
| P2 Processing | **Built + tested** (26 unit tests incl. 60/90/72 bpm, full-history cross-check) |
| P3 Cloud + online + switching | **Built + tested locally** (switch 5.2 s / 1.9 s; latency max 3.3 s) |
| P4 Offline + catch-up | **Built + tested locally** (2 h backlog in 17 s) |
| P5 Failure tests on ARM64 | Scripts written (`tests/chaos/`), **not yet run** — needs ARM64 env + official mock |
| P6 Deliverables | README, design note draft, evidence log started; video + final figures pending |

Blocked on: ARM64 environment, cloud VM, official mock download link.

---

## 0. Scoring strategy

| Area | Points | Our priority |
|---|---|---|
| Data safety on device (criteria 1–3) | 35 | 1st — built and tested first |
| Online mode + switching (criteria 4–5) | 25 | 3rd |
| Complete cloud copy (criterion 6) | 20 | 2nd — falls out of the storage design |
| Correct results (criterion 7) | 10 | 4th |
| Design note, code quality, resources (criterion 8) | 10 | Continuous |
| Deductions: −5 per silent loss / unflagged wrong value (max −30) | | Zero tolerance: verify after every change |
| **Paid service = disqualified** | | Hard rule (§11) |

Rule of thumb: **never be wrong silently**. A recorded loss costs little; a hidden one costs 5.

---

## 1. Environments

### 1.1 Development device (ARM64, Raspberry Pi-class)
- **Primary:** Oracle Cloud Always-Free Ampere A1 VM (ARM64), Ubuntu 22.04/24.04 or Debian 12.
- **Fallback:** QEMU Raspberry Pi OS (64-bit) image on the laptop.
- **Enforce the resource limits** (criterion 8), and prove it in the evidence:
  - All our processes and the mock run inside a systemd slice `hb.slice`
    with `CPUQuota=400%` and `MemoryMax=2G`, or the VM is created with 4 OCPUs / 2 GB RAM.
  - Check with `nproc`, `free -m`, `uname -m` (must print `aarch64`). Save the output to `evidence/env.txt`.
- **The mock and the pipeline both run on this machine** (§1 of the brief).
- Python 3.11+ and SQLite ≥ 3.35 (stock on Raspberry Pi OS Bookworm, so a fresh Pi needs no installs).

### 1.2 Final target
- Judging runs on a **real Raspberry Pi 4** plus **our cloud account**. Therefore:
  - The install must work on a fresh Raspberry Pi OS 64-bit (§10.1).
  - The cloud must stay up and reachable for the whole judging period. We set a health
    check / uptime monitor (free, e.g. UptimeRobot) and use auto-restart on the cloud VM.
  - If possible, do one full dress rehearsal on a physical Pi 4.

### 1.3 Cloud (free tier only)
- **Choice:** one small free VM (Oracle Always Free AMD/ARM, or GCP e2-micro Always Free) running:
  - our ingest API (Python stdlib HTTP server) + SQLite (WAL, `synchronous=FULL`);
  - the cloud processing worker, which imports **the same** `processing.py` as the device.
- **Why not serverless:** Cloudflare D1's free plan allows ~100k row writes/day; we produce ~2.9M
  samples/hour. Workers' free plan allows 100k requests/day; one upload + one health check per second
  is 172k/day. AWS's newer free plan is credit-based, so it can run into charges.
- **TLS:** Caddy (open source) + free hostname (sslip.io / DuckDNS) gives automatic Let's Encrypt HTTPS.
- **Auth:** a shared bearer token in the device config.
- **Billing guard:** no paid add-ons; disable/avoid anything billable; budget alert at $0.01 if
  the provider offers it; screenshot the "Always Free" status for the evidence pack.

### 1.4 The mock
- Download `biosignal-mock` + `check_ground_truth` (ARM64) as soon as the link is given.
  **Never modify them** (rule).
- Until then, we develop against `tools/fake_mock.py`, our own reimplementation of §6 of the brief
  (ramp/signal modes plus every listed fault). **All final tests and evidence use the real mock.**
- Note: the mock allows **one client at a time**. Our pipeline holds a single connection, and
  nothing else (status/export commands) talks to the mock.

---

## 2. Architecture

```
            Raspberry Pi 4 (≤4 cores, ≤2 GB)                         Free cloud VM
 ┌──────────────────────────────────────────────────────┐      ┌───────────────────────────┐
 │ biosignal-mock :7100                                  │      │ Caddy (HTTPS)             │
 │      │ NDJSON/TCP (one client)                        │      │   │                       │
 │ ┌────▼─────────┐  commit/200ms   ┌──────────────────┐ │HTTPS │ ┌─▼──────────┐  ┌────────┐ │
 │ │ Collector    ├────────────────►│ SQLite (WAL,FULL)│ │─────►│ │ Ingest API ├─►│ SQLite │ │
 │ │ CRC, dedup,  │                 │ chunks, losses,  │ │      │ └────────────┘  └───┬────┘ │
 │ │ gaps, session│                 │ cursors, results │ │      │ ┌────────────────┐  │      │
 │ └──────────────┘                 └──┬─────────┬─────┘ │      │ │ Online worker  │◄─┘      │
 │ ┌──────────────┐  reads committed   │         │       │      │ │ processing.py  │         │
 │ │ Uploader     │◄───────────────────┘         │       │      │ └────────────────┘         │
 │ │ live+backlog │                              │       │      └───────────────────────────┘
 │ ├──────────────┤                    ┌─────────▼─────┐ │
 │ │ Health/Mode  │ 1 s pings          │ Offline worker│ │
 │ └──────────────┘                    │ processing.py │ │
 │  CLI: pipeline status|export|results └───────────────┘ │
 └──────────────────────────────────────────────────────┘
```

- One program, `pipeline`, runs as the systemd service `hbpipe.service` with these threads:
  collector, uploader, health/mode manager and offline processor.
- CLI subcommands read the database (WAL allows concurrent readers) and, for `--from cloud`,
  call the cloud API.

---

## 3. Data model and invariants

### 3.1 Device database (SQLite, WAL, `synchronous=FULL`)
| Table | Key | Purpose |
|---|---|---|
| `sessions` | session_id | seq, boot_id, end_1, end_2, dev_next_window, cloud_done, uploaded |
| `chunks` | (session, sensor, first_index) | ≤400 samples, packed int32 LE, `uploaded` flag |
| `losses` | (session, sensor, from_index) | to_index (NULL = unknown end), reason, `uploaded` flag |
| `cursors` | (session, sensor) | next index not yet accounted for |
| `results` | (session, window, mode) | offline results, `uploaded` flag |
| `meta` | key | mode, mode_since, counters |

### 3.2 Invariants (the backbone of criteria 1, 2, 5, 6)
1. **Coverage:** for every (session, sensor), the range `[0, cursor)` is covered **exactly once**
   by stored chunks ∪ recorded losses. No overlaps, no holes.
2. **Atomicity:** a chunk or loss and its cursor advance are written in **one transaction**.
   After any crash, the database is in a state where invariant 1 holds.
3. **Immutability:** chunks never change after commit, so a re-upload is byte-identical and the
   cloud can deduplicate on its key.
4. **Ack after durability:** the device sets `uploaded=1` only after the cloud has committed and
   replied. Losses/sessions are re-marked un-uploaded if they change later (conditional update on
   the values sent).
5. **No invention:** no value is ever interpolated or filled. Missing = absent + listed.

### 3.3 Cloud database
Same `chunks` / `losses` / `sessions` layout, plus `results(session, window, mode, body, created_at)`.
`INSERT OR IGNORE` for chunks; upsert for losses and session ends. The cloud keeps in-memory interval
coverage per (session, sensor), rebuilt from the database at startup.

---

## 4. Phase P1 — Collect and save (criteria 1–3, 35 pts)

### 4.1 Reading
- One TCP connection to the mock. Each sensor is polled every 100 ms (immediately again if
  the reply was full, i.e. 400 samples). Well inside the 10 s buffer.
- **Always send `from_index = cursor`** once the session is known. Pipeline restarts, duplicate
  replies and corrupt replies all become harmless re-reads.
- Treat every received line as an independent message and apply it by its own sensor/indices. A
  duplicate or late reply is then deduplicated naturally, and the request/reply pairing cannot desync.
- **Timeouts:** 2 s per reply. After 3 consecutive timeouts, reconnect with backoff (handles
  "stop answering for a while"). All timing uses `time.monotonic()`.

### 4.2 Validating (§6.2 / §6.3 of the brief)
- **CRC:** locate the raw `data` substring in the received line (`[` to matching `]`) and compute
  zlib CRC-32 on those exact bytes. Never re-serialise. On mismatch: discard, re-read from cursor.
- Check shape: `count == len(data)`, 3/2 channels, integers within the 24-bit range. Otherwise treat as corrupt.
- Ignore and log lines that are not JSON or that carry an error; never crash on them.

### 4.3 Applying a reply (per sensor, cursor `c`)
Events = reported `lost` ranges + the data range, sorted by start index:
- An event that ends before `c` is a duplicate → skip. An event that overlaps `c` → trim.
- A `lost` range → record a loss `[max(from,c), to]` with the mock's reason; advance the cursor.
- Data → save rows from `c`; advance the cursor.
- **Unexplained gap** (`start > c`) → do **not** record a loss yet. Re-request from `c`.
  Record `not_delivered_by_mock` only after 5 failed re-reads of the same gap.
- Contiguous losses with the same reason are merged (e.g. a long `disk_full` or dropout).

### 4.4 Sessions
- New `session_id` (not seen before) → the mock restarted. For each sensor of the old session
  that did not end, record a loss `{from_index: cursor, to_index: null, reason}`:
  - reason `device_power_loss_or_reboot` if the Linux `boot_id` changed since that session was
    recorded, else `mock_restart` (or the mock's own reason if a null-ended `lost` entry is supplied).
- Replies from an already-closed session (stale duplicates) are ignored.
- `session_end: true` → mark the sensor ended at that index. No tail loss.
- Indexes restart at 0 for the new session: the cursor resets, and a gap from 0 is re-read.

### 4.5 Durability
- Batch commits every **200 ms** in one transaction (WAL + `synchronous=FULL` ⇒ fsync per commit).
- `kill -9` / crash: restart in ~1 s (systemd). Resume from the committed cursor; the mock still
  holds 10 s ⇒ **zero loss**.
- Reboot: same, plus `hbpipe.service` is `WantedBy=multi-user.target`. The service waits for the
  mock (retrying the connection) rather than failing.
- **Power cut:** the mock dies too. Lost = samples produced but not committed. Worst case is
  poll interval (100 ms) + commit interval (200 ms) + fsync time ≈ **0.35 s**.
  - **Stated bound: 0.5 s** (finalised after measurement: stated = measured worst case with margin, ≤ 1 s).
  - These samples are **listed** as the old session's tail loss (`to_index: null`), never silent.
- Caveats for the design note: SD-card controllers may lie about flush. We use ext4
  (`data=ordered`, default) and recommend a quality card / USB SSD.

### 4.6 Disk full (judges fill the disk)
- At start, keep a 16 MB **reserve file** in the data directory.
- Check free space every 1 s. If it drops below the low-water mark, or SQLite reports `SQLITE_FULL`:
  1. delete the reserve;
  2. retry the pending batch (normally it succeeds);
  3. enter **disk-full mode**: keep reading the mock (so its losses stay truthful) but store only
     merged `disk_full` loss records (tiny), using the reserve space.
- Every 5 s, if free space > reserve + 32 MB: recreate the reserve and resume saving data.
- Never delete data the cloud has acknowledged: the device export must stay complete.
- If the disk is full **at boot**: the service still starts, and enters disk-full mode immediately.

### 4.7 Clock changes
- Ordering = sample index. Timeouts = monotonic clock. Latency stamps = cloud clock.
- Wall time on the device is informational only (logs). Tests jump the clock ±1 h and ±1 day.

### 4.8 Exit tests for P1
`tools/verify_export.py` (and `check_ground_truth` once available), run against the ramp mock:
- 10 min clean run → all samples present, unchanged, exactly once.
- corrupt + duplicate + dropout + stall faults → losses only where the mock reported them.
- `kill -9` × 10 at random times → zero loss.
- mock restart → old tail listed with `to_index: null`.

---

## 5. Phase P2 — Processing logic (criterion 7, 10 pts)

In `hbpipe/common/processing.py`, as pure functions of `(loader, window)`. Used unchanged by device and cloud.

| Output | Rule implemented |
|---|---|
| `missing_1`, `missing_2` | 400 − available samples of that sensor in `[400w, 400w+399]` |
| `complete` | both missing = 0 |
| `mean_*`, `min_*`, `max_*` | per channel, over available samples; `null` if none; mean computed exactly (rational), rounded to 1 dp |
| `magnitude` | mean of √(ch1²+ch2²) over available sensor-2 samples (`math.fsum`), 1 dp; `null` if none |
| `heart_rate` | §5.1 |
| `mode` | `online` (cloud) / `offline` (device) |
| plus | `session`, `window`, `processed_at` (cloud clock for online results) |

### 5.1 Heart rate (sensor 1, channel 2)
- Peak at `i` ⇔ samples `i−800 … i+1` available **and** `x[i] > x[i−1]` **and** `x[i] > x[i+1]`
  **and** `2·x[i] > max(x[i−800 … i−1])` (exact integer form of "> half the max")
  **and** `i − previous_peak ≥ 100`.
- Rolling max via a monotonic deque (O(n)). The availability run resets at gaps.
- HR for window `w`: `null` if `w < 4`, or any sample in `[400(w−4), 400(w+1)−1]` is missing, or
  fewer than 2 peaks in that span. Else `round(24000·(k−1) / (p_last − p_first), 1)`, which equals
  60 ÷ mean gap in seconds, computed exactly.
- Previous-peak chain: warmed up from 2000 samples before the span (inputs needed:
  `[400(w−4) − 2800, 400(w+1)]`). This is identical to the full-history chain whenever two candidate
  peaks are ever ≥100 apart (always true for ECG). A test compares it against a full-history
  reference implementation.
- Rounding: half away from zero, done on exact rationals, so there are no float artefacts.

### 5.2 When a window may be computed ("final inputs")
Window `w` is computed **only** when every input index is accounted for (data **or** recorded
loss **or** past session end):
- sensor 2 over `[400w, 400w+399]`;
- sensor 1 over `[400(w−4) − 2800, 400(w+1)]`.

Otherwise samples still in transit would be counted as missing. That would be an unflagged wrong
value (−5).

### 5.3 Exit tests for P2
- Unit tests: hand-built windows (all present, partial, empty, gaps at the span edges).
- Signal mock: HR = 60 / 90 / 72 bpm in the right minutes. Channels 1/3 = ch2 × 0.6 / × 1.3 sanity check.
- Ramp mock: exact means/min/max by formula.
- Device-computed and cloud-computed results for the same data are byte-identical.
- Later: compare with the organisers' reference if they share sample outputs.

---

## 6. Phase P3 — Cloud and online mode (criteria 4–5, 25 pts)

### 6.1 Cloud API
| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness (used for mode detection) |
| `POST /ingest` | gzip JSON: sessions, chunks, losses, offline results. Committed in one transaction, **then** acked. Reply carries `done_upto` per session |
| `GET /export?session=` | cloud copy in the export format (§9) |
| `GET /results?session=` | all results (online and offline uploads) |
| `GET /status?session=` | counts for `pipeline status` |

### 6.2 Upload
- Every 0.5 s while online. Uses a persistent HTTPS connection with timeouts, wrapped in a hard
  deadline (covers DNS hangs).
- Batch priority: session updates → losses → **offline results** → **live lane** (newest
  un-uploaded 30 s) → **backlog lane** (oldest first, ~2 MB per request). Backlog is sent only once
  all offline results are sent.

### 6.3 Online processing and the 5 s rule
- The cloud worker runs every 200 ms and computes every window whose inputs are final (§5.2) and
  which has no result yet.
- Latency budget: poll 0.1 s + commit 0.2 s + upload 0.5 s + network ~0.2 s + worker 0.2 s +
  compute ≈ **1.5 s ≤ 5 s**.
- `processed_at` is stamped with the cloud clock. Evidence: a latency histogram from 1 h online.

### 6.4 Mode switching (≤10 s, no loss / no duplicate)
- Health ping every 1 s with a 1.5 s hard timeout. Upload failures count as failed pings.
- online → offline after **3** consecutive failures (worst case ≈ 1 + 3×1.5 = **5.5 s**).
- offline → online after **2** consecutive successes (≈ **2 s**).
- **Samples cannot be lost or duplicated by a switch:** the collector never depends on the mode
  (it always saves locally), and the cloud deduplicates on immutable chunk keys.
- **Windows (no gaps, no double work):** the device processes offline from
  `max(dev_next, cloud_done+1)`; on reconnect its offline results reach the cloud **before** the
  backlog data, and the cloud skips windows that already have an offline result. At most a
  couple of boundary windows get both an `online` and an `offline` line; that is allowed, since
  the output is "one line per window and mode".
- Every switch is logged to `events.jsonl` with a reason, for the evidence pack.
- Variants tested: iptables DROP (packets silently dropped), interface down (immediate errors),
  DNS failure, cloud process stopped, flapping network (hysteresis prevents thrash).

---

## 7. Phase P4 — Offline mode and catch-up (§2.3, §2.5, criterion 6, 20 pts)

- **Offline:** the device runs the same processing for windows whose inputs are final locally,
  stores the results with `mode: offline`, and uploads them later.
- **Catch-up sizing:** 2 h outage = 2 × 3600 × 400 × 2 sensors = **5.76 M samples**.
  - Packed size ≈ 57.6 MB raw, ~35–45 MB gzipped.
  - 15 min ⇒ needs ≈ 50 KB/s. Target ≤ 5 min with 2 MB batches.
- **Exactly once:** immutable chunks + `INSERT OR IGNORE` + ack-after-commit. A device crash
  mid-upload only causes harmless re-sends.
- The live lane keeps online results flowing **during** catch-up, so the 5 s rule holds for new seconds.
- **Exit test:** a 2 h outage (real time, on the ARM64 env) → cloud export equals the device export
  and the ground truth in ≤ 15 min. Record the actual duration and throughput.

---

## 8. Phase P5 — Failure testing and evidence (criteria 1–6; deliverable 3)

Each test is a script in `tests/chaos/`, run on the ARM64 env with the **real mock**. It saves
logs, `status` snapshots, and `check_ground_truth` output to `evidence/<test>/`.

| # | Test | Method | Pass condition |
|---|---|---|---|
| T1 | Clean 1 h, ramp | – | device and cloud exports = ground truth |
| T2 | Process kill | `kill -9` every 30–120 s, 20× | zero loss, zero duplicates |
| T3 | Reboot | `sudo reboot` mid-run | only the mock-restart tail lost, listed |
| T4 | Power cut | physical Pi: pull the plug; VM: `virsh destroy` / QEMU `system_reset` / force-stop | listed loss ≤ stated bound; database intact |
| T5 | Network cut 30 s / 10 min / 2 h | `iptables -A OUTPUT -d <cloud> -j DROP`; also `ip link down` | switch ≤ 10 s both ways; cloud complete ≤ 15 min |
| T6 | Network flapping | 5 s on/off × 20 | no loss/dup; no window without a result |
| T7 | Disk full | data dir on a 64 MB loop mount, fill with `fallocate` | `disk_full` losses listed; recovery after freeing space |
| T8 | Clock change | `date -s` ±1 h / +1 day during a run | no effect on data or results |
| T9 | Mock faults | real mock fault modes (corrupt / dup / stall / dropout / restart) | only mock-reported losses |
| T10 | Results | signal mode 10 min, online vs offline vs reference | identical; HR 60/90/72 |
| T11 | Combined | net cut + kill + clock jump in one run | all of the above |

Evidence summary: `docs/EVIDENCE.md` — table of test, date, environment, result and link to logs.

---

## 9. Required commands (exact formats)

The single program `pipeline` runs on the device.

```
pipeline status
→ {"mode": "online", "session": "5f0c2a9e", "samples_saved": 2880000, "samples_in_cloud": 2879200, "backlog": 800, "losses": 0, "disk_full": false}

pipeline export --session ID --from device|cloud
→ one line per sample, ordered by sensor then index:
  {"sensor": 1, "index": 120000, "values": [720, 1200, 1560]}
→ then one line per loss:
  {"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"}

pipeline results --session ID [--from device|cloud|all]
→ one line per (window, mode) with all §5 fields
```
- `--session latest` is accepted for convenience.
- `samples_in_cloud` comes from acked counts, so it works offline too.
- Exports stream (constant memory) and handle 2 h sessions.
- **Plus:** `pipeline run` (the service) and `pipeline sessions` (list).

---

## 10. Phase P6 — Deliverables (§5 of the brief)

### 10.1 Repository + README (fresh board)
- `deploy/install-device.sh`:
  - creates the user and the data dir (`/var/lib/hbpipe`) and writes `/etc/hbpipe/pipeline.env`
    (cloud URL, token);
  - installs `hbpipe.service` (`Restart=always`, `RestartSec=1`, enabled at boot,
    `CPUQuota=400%`, `MemoryMax=2G`) and the `/usr/local/bin/pipeline` wrapper.
- `deploy/install-cloud.sh`: cloud service + Caddy.
- README: prerequisites, 5-command quick start, how to run the mock, how to run each test,
  how to read status, troubleshooting.
- Code quality: type hints, docstrings on invariants, unit tests (`python -m unittest`), no dead code.

### 10.2 Design note (≤ 2 pages) — `docs/DESIGN.md` → PDF
1. Architecture diagram + one paragraph per component.
2. **Guaranteed:** exactly-once storage of every received sample; every non-stored sample listed
   with a reason; crash/kill → zero loss; switch ≤ 10 s; identical results online and offline.
3. **Not guaranteed:** samples the mock never delivered; the last ≤ 0.5 s before a power cut
   (listed); SD-card hardware that ignores flush; outages longer than the disk capacity
   (~X days at 29 MB/h, stated).
4. **Maximum power-cut loss: 0.5 s, and why** (§4.5 arithmetic + measured value).
5. Assumptions (§14) and the free-tier compliance statement.

### 10.3 Evidence of failure tests — `docs/EVIDENCE.md` + `evidence/` logs (§8)

### 10.4 Resource figures (1 h online, ARM64 env)
Measured with `tools/resource_monitor.py` sampling `/proc` every 5 s:
- **average CPU %** (utime+stime of the pipeline process tree / wall time / cores; also per-core);
- **peak memory** (`VmHWM` of each process, summed);
- **MB written to disk** (`/proc/<pid>/io` `write_bytes`);
- **MB sent over the network** (interface tx bytes from `/proc/net/dev` + app-level counter).

Report the environment explicitly (e.g. "Oracle A1, 4 OCPU limited, 2 GB, Ubuntu 24.04 aarch64",
and Pi 4 if measured). Expected: < 10 % CPU, < 80 MB RAM, ~30 MB/h disk, ~20 MB/h network.

### 10.5 Demo video (≤ 5 min)
| Time | Content |
|---|---|
| 0:00–0:30 | Architecture, `uname -m`, limits, `pipeline status` |
| 0:30–1:45 | **Network cut:** status shows online → offline (timer on screen); offline results appear; restore → online; backlog drains |
| 1:45–2:45 | **Process kill:** `kill -9`, systemd restart, status continues, no gap |
| 2:45–4:15 | **Power cut:** pull the power (physical) / force stop (VM), boot, status, loss record shown |
| 4:15–5:00 | `check_ground_truth` on device + cloud exports → pass; results sample |

### 10.6 Rules compliance
- Free services and open-source software only. List every component and its licence in the README.
- Mock unmodified (checksum recorded).
- Synthetic data only.
- **AI disclosure:** section in the README naming the AI tools used and for what.

---

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Free cloud signup needs a card → accidental billing → disqualification | Always-Free shapes only, no paid add-ons, budget alert, screenshot proof |
| Cloud VM down during judging | systemd restart, uptime monitor, keep the VM idle-safe (Oracle reclaims idle VMs: keep a light cron) |
| Real mock behaves differently from the fake one (e.g. reply to `from_index`) | Test with the real mock the day it arrives; protocol code is defensive (gap re-read, stale-session ignore) |
| HR ambiguity vs the reference | Open questions to organisers (§14); full-history cross-check test |
| SD-card latency spikes on fsync | 200 ms batching; measure on the Pi; WAL |
| Python CPU on the Pi | Regex-based CRC slice, packed arrays, batch SQL; measure early |

---

## 12. Timeline

Duration is blank in the brief. Split the time by percentage:

| Phase | Share |
|---|---|
| P0 Env + cloud + mock running | 10 % |
| P1 Collect + save | 25 % |
| P2 Processing | 12 % |
| P3 Cloud + online + switching | 18 % |
| P4 Offline + catch-up | 10 % |
| P5 Failure tests + resource run (incl. 2 h outage, 1 h benchmark) | 15 % |
| P6 Docs, video, polish | 10 % |

Long-running tests (2 h outage, 1 h benchmark) run in the background while writing docs.

---

## 13. Traceability matrix (every requirement → plan → proof)

| Brief § | Requirement | Plan § | Proof |
|---|---|---|---|
| 1 | ARM64 Pi-class env, ≤4 cores, ≤2 GB; mock + pipeline on it | 1.1 | `evidence/env.txt`, systemd limits |
| 1 | Free cloud service receives, stores, processes | 1.3, 6 | billing screenshot, T1 |
| 1 | Works on a real Pi 4 + our cloud account | 1.2, 10.1 | fresh-board install rehearsal |
| 2.1 | Read both sensors continuously | 4.1 | T1 |
| 2.1 | Save every sample on disk | 4.3, 4.5 | T1 |
| 2.1 | Survive crash, reboot, power cut | 4.5 | T2, T3, T4 |
| 2.2 | Stream to cloud continuously | 6.2 | T1 |
| 2.2 | Cloud result per second within 5 s | 6.3 | latency histogram (T1) |
| 2.3 | Offline: keep collecting + process on device, same logic | 7, 5 | T5, T10 |
| 2.4 | Switch both ways within 10 s | 6.4 | T5, T6 (events.jsonl timings) |
| 2.4 | No loss/duplication due to a switch | 3.2, 6.4 | T5, T6 |
| 2.5 | Catch-up: cloud gets every sample exactly once | 7 | T5 (2 h) |
| 2.6 | Record lost samples exactly (sensor, range, reason) | 4.3, 4.4, 4.6 | T7, T9 |
| 2.6 | Never fill gaps with invented values | 3.2 (5), 5.2 | T9, code review |
| 2 | Any language/tools; free | 1 | README licence list |
| 3 | 1 s windows `400w … 400w+399` | 5 | unit tests |
| 3 | missing_1/2, complete | 5 | unit tests |
| 3 | mean/min/max per channel, null if none | 5 | unit tests, T10 |
| 3 | magnitude | 5 | unit tests |
| 3 | heart rate (peak rules 1–4, rate, null rules) | 5.1 | unit tests, T10 (60/90/72) |
| 3 | mode online/offline | 5, 6.4 | T5 |
| 3 | Round to 1 decimal | 5.1 | unit tests |
| 4 c1 | On disk exactly once, unchanged | 3.2, 4 | `check_ground_truth` device export |
| 4 c2 | Every missing sample listed with a reason | 3.2, 4.3 | same |
| 4 c3 | Crash/reboot/power cut; stated power-cut loss | 4.5 | T2–T4, design note |
| 4 c4 | ≤5 s online results | 6.3 | latency histogram |
| 4 c5 | Switch ≤10 s, no loss/dup | 6.4 | T5, T6 |
| 4 c6 | Outage ≤2 h → cloud complete ≤15 min | 7 | T5 2 h timing |
| 4 c7 | Results match reference | 5 | T10 |
| 4 c8 | Within limits; CPU/mem/disk/net reported | 1.1, 10.4 | resource report |
| 4 | `pipeline status` one JSON line | 9 | CLI test |
| 4 | `pipeline export --from device/cloud` format | 9 | `check_ground_truth` |
| 4 | `pipeline results` per window and mode | 9 | CLI test |
| 4 | No silent loss / unflagged wrong value (deductions) | 3.2, 5.2 | every test runs the ground-truth check |
| 4 | No paid service (disqualification) | 1.3, 11 | billing screenshot |
| 5.1 | Repo + README to install on a fresh board | 10.1 | rehearsal on clean image |
| 5.2 | Design note ≤2 pages: architecture, guarantees, max power-cut loss + why | 10.2 | PDF page count |
| 5.3 | Failure test evidence + results | 8 | `docs/EVIDENCE.md` |
| 5.4 | 1 h online resource figures + environment named | 10.4 | resource report |
| 5.5 | Video ≤5 min: network cut, process kill, power cut | 10.5 | video |
| 5 rules | Free/open-source only; mock unmodified; synthetic data; AI disclosed | 10.6 | README sections |
| 6.2 | NDJSON/TCP, one client at a time | 1.4, 4.1 | T1 |
| 6.2 | `session_id` change → indexes restart at 0 | 4.4 | T3, T9 |
| 6.2 | `first_index`, `count`, `lost`, `session_end` handling | 4.3, 4.4 | T9 |
| 6.2 | CRC-32 of exact data text; corrupt replies not used | 4.2 | T9, unit tests |
| 6.2 | Re-read with `from_index` | 4.1 | T2, T9 |
| 6.2 | 10 s buffer; overflow reported in `lost` | 4.1, 4.3 | T9 |
| 6.3 | Mock stops answering | 4.1 | T9 |
| 6.3 | Corrupt reply | 4.2 | T9 |
| 6.3 | Same reply twice | 4.1, 4.3 | T9 |
| 6.3 | One sensor stops (reported in lost) | 4.3 | T9 |
| 6.3 | Mock restart → old unread listed, `to_index: null` | 4.4 | T9, T3 |
| 6.3 | Judges: network cut, kill, power, disk full, clock change | 4.5–4.7, 6.4 | T2–T8 |
| 6.3 | Use `check_ground_truth` before submission | 8 | all tests |
| 6.1 | ramp mode exact values; signal mode HR schedule | 5.3 | T1, T10 |

---

## 14. Open questions for the organisers (assumptions until answered)

1. **Peak chain:** does "≥100 samples after the previous peak" carry across gaps and from the
   session start? *Assumed: yes (see §5.1).*
2. **Stats field names/shape:** *assumed `mean_1`/`min_1`/`max_1` lists per channel, `null` when
   none.* Is a fixed schema expected by the reference comparison?
3. **Rounding:** *assumed half away from zero.* Do min/max (integers) stay integers?
4. **Results after catch-up:** should the cloud also produce `online` results for windows already
   processed offline? *Assumed no.*
5. **Session tail on restart:** does the mock itself report the old session's unread range, or
   must the pipeline infer it? *We infer it, and use the mock's entry if present.*
6. The duration and date fields are blank, and the mock download link is missing.
