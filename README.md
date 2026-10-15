# hbpipe — Never Lose a Heartbeat

A data pipeline for a bedside device on a Raspberry Pi-class ARM64 board. It saves every sample
from two 400 Hz sensors durably. It streams the data to a free cloud VM, which processes it within
seconds. When the network drops, the device processes the data itself and then catches up. Every
sample that could not be saved is listed with its index range and a reason; no gap is ever filled
with invented values.

- Design: [docs/DESIGN.md](docs/DESIGN.md) (2-page design note)
- Plan and requirement traceability: [docs/PLAN.md](docs/PLAN.md)
- Test evidence: [docs/EVIDENCE.md](docs/EVIDENCE.md)

## Where it runs

| Side | Used for this submission | Also supported |
|---|---|---|
| Device | **Raspberry Pi 4** (judging). Development/evidence: Docker `linux/arm64` Debian 12 container limited with `--cpus 4 --memory 2g` (`docker/Dockerfile.device`) | any ARM64 Linux with Python ≥ 3.9 and SQLite ≥ 3.24 (both ship with Raspberry Pi OS Bookworm) |
| Cloud | **Render free web service + Neon free Postgres** (no payment method on either) | any free VM with SQLite (`deploy/install-cloud.sh`) |

The device uses only the Python standard library. The cloud adds one open-source driver,
`psycopg`, for Postgres.

## Install

### 1. Cloud (once): see [docs/DEPLOY.md](docs/DEPLOY.md)
Render Blueprint (`render.yaml`) plus a Neon connection string. About 20 minutes, no card needed.
Check with `curl https://<service>.onrender.com/health`.
- Render's free tier sleeps after 15 minutes without requests. **Open `/health` once before
  starting the device.** While the cloud is waking, the device runs offline and then catches
  up, as designed.

### 2. Device (fresh board)
```bash
git clone <this repo> && cd hbpipe
sudo ./deploy/install-device.sh https://<your-hostname> <same-token>
```
This installs `/usr/local/bin/pipeline` and the `hbpipe.service` systemd unit:
- the unit restarts within 1 s and starts at boot;
- it is limited to `CPUQuota=400%` and `MemoryMax=2G`;
- configuration lives in `/etc/hbpipe/pipeline.env`.

### 2b. Device as an ARM64 container (how our evidence was produced)
```bash
docker build --platform linux/arm64 -f docker/Dockerfile.device -t hbpipe-device .
docker run -d --name hb-dev --platform linux/arm64 --cpus 4 --memory 2g --cap-add NET_ADMIN \
  -v hb-data:/data -e HB_CLOUD_URL=https://<service>.onrender.com -e HB_CLOUD_TOKEN=<token> hbpipe-device
docker exec hb-dev pipeline status
```
The container runs the mock and a supervisor loop that restarts the pipeline after 1 s, in place
of systemd. Full failure suite: `python tests/arm64_suite.py kill net power disk clock signal catchup resources`.

### 3. Mock
Run the provided `biosignal-mock` on the same board, e.g.
`biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100`.
The pipeline connects to `127.0.0.1:7100` and retries until the mock is up.

## Commands

```bash
pipeline status                                    # one JSON line
pipeline export --session <ID|latest> --from device|cloud > export.jsonl
pipeline results --session <ID|latest> [--from device|cloud|all]
pipeline sessions                                  # list recorded sessions
journalctl -u hbpipe -f                            # logs
```
- `status` prints `{"mode": "online", "session": "…", "samples_saved": N, "samples_in_cloud": M, "backlog": N-M, "losses": k, "disk_full": false}`.
- `export` prints one line per sample, ordered by sensor and index, then one line per loss.

## Tests

| What | Command | Where |
|---|---|---|
| Unit tests (26) | `python3 -m unittest tests.test_protocol tests.test_processing tests.test_collector` | anywhere |
| End-to-end: faults, network cut, 2× kill -9 | `python3 tests/e2e_local.py` | anywhere (uses `tools/fake_mock.py`) |
| End-to-end: mock restart | `python3 tests/e2e_local.py --scenario restart` | anywhere |
| End-to-end: ECG signal + latency | `python3 tests/e2e_local.py --mode signal` | anywhere |
| 2-hour catch-up benchmark | `python3 tools/bench_catchup.py --hours 2 [--cloud-url …]` | device |
| Failure tests T2–T8 | `tests/chaos/*.sh` (see headers) | device, as root, real mock |
| Power cut | [tests/chaos/power_cut.md](tests/chaos/power_cut.md) | device |
| Resource figures (1 h) | `tools/resource_monitor.py --pid … --iface … --seconds 3600` | device |
| Export check | `python3 tools/verify_export.py export.jsonl --mode ramp --seed 7` | anywhere |

**About the mock:** the download link for `biosignal-mock` / `check_ground_truth` was blank in
the assessment we received. We therefore built `tools/fake_mock.py` strictly from section 6 of
the brief: protocol, CRC rule, 10 s buffer, ramp/signal modes, and every listed fault. We built
`tools/verify_export.py` as the ground-truth check. The pipeline depends only on the documented
protocol, so it runs unchanged against the official mock, which we never modify.

## Layout

```
hbpipe/common/   protocol (CRC on raw text), codec, processing (shared by device and cloud), DB helpers
hbpipe/device/   collector, store, uplink (mode switching + uploader), offline processor, CLI
hbpipe/cloud/    ingest API, cloud store + online worker
tools/           fake mock, export verifier, cut proxy, resource monitor, catch-up benchmark
tests/           unit tests, local end-to-end test, chaos/ failure scripts for the device
deploy/          systemd units, install scripts, Caddyfile
```

## Free services and open-source software only

| Component | Licence / tier |
|---|---|
| Python 3, SQLite | PSF licence / public domain |
| psycopg 3 (cloud only) | LGPL 3 |
| PostgreSQL, Debian, Docker Engine | open source |
| Render web service | free plan, no payment method |
| Neon Postgres | free plan, no payment method |


