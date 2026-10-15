# Never Lose a Heartbeat: submission

**Candidate:** Harsh · **Repository:** https://github.com/Harsh202458/mayacardio-hbpipe ·
**Cloud (live):** https://hbpipe-cloud.onrender.com (`/health` is public; data endpoints need the token)

## Contents of this zip

| Deliverable (brief §5) | Where |
|---|---|
| 1. Code + README to install on a fresh board | `1_code/` (identical to the GitHub repo). Start with `1_code/README.md`. |
| 2. Design note (≤ 2 pages): architecture, guarantees, maximum power-cut loss | `2_Design_Note.pdf` |
| 3. Evidence of the failure tests and their results | `3_evidence/EVIDENCE.md` (summary), `3_evidence/arm64/<test>/summary.json`, plus event logs and results |
| 4. Resource figures over 1 h in online mode (CPU, memory, disk MB, network MB) + environment | `4_resources/resources-1h.json` (summary in `3_evidence/EVIDENCE.md`) |
| 5. Demo video ≤ 5 min (network cut, process kill, power cut) | `5_demo_video.mp4` |

## Quick start for the judges (Raspberry Pi 4, Raspberry Pi OS 64-bit)

```bash
git clone https://github.com/Harsh202458/mayacardio-hbpipe && cd mayacardio-hbpipe
sudo ./deploy/install-device.sh https://hbpipe-cloud.onrender.com <TOKEN>   # token sent on request
# start the official mock on port 7100, then:
pipeline status
pipeline export --session latest --from device     # also: --from cloud
pipeline results --session latest
```

- The device needs nothing beyond stock Raspberry Pi OS (Python 3 standard library and SQLite).
- `hbpipe.service` restarts within 200 ms, starts at boot, and is capped at 4 CPUs and 2 GB.
- Render's free tier sleeps after 15 minutes idle. **Please open https://hbpipe-cloud.onrender.com/health once before starting.** While it wakes (about 1 min), the device simply runs offline and then catches up, as designed.

## Key design points (details in the design note)

- **No silent loss by construction.** For every sensor, the indices up to the cursor are covered exactly once by stored samples and recorded losses. Data and cursor are committed in the same transaction every 200 ms, on a dedicated writer thread.
- **CRC-32 on the exact received `data` text.** Every request carries `from_index`, so duplicates, corrupt replies and restarts become harmless re-reads. Mock restarts are listed as tail losses with `to_index: null`.
- **Exactly once in the cloud.** Immutable blocks are keyed (session, sensor, first_index), and data counts as uploaded only after the cloud's commit.
- **One processing module, used online and offline.** Exact rational rounding; a window is computed only when its inputs are final.
- **Stated maximum power-cut loss: 0.5 s per sensor.**

## Environment used for the evidence

No Raspberry Pi was available, so the device ran as a **linux/arm64 Debian 12 container** (same base as Raspberry Pi OS Bookworm) under Docker Desktop, limited with `--cpus 4 --memory 2g`. ARM is emulated on an x86-64 Windows PC. This makes CPU figures and restart times pessimistic: the same code starts in 0.13–0.36 s natively.

The cloud is **Render (free web service, Ohio) + Neon Postgres (free, AWS us-east-2)**. No paid service and no payment method is used.

## About the mock

The `biosignal-mock` download link was blank in the assessment we received. We implemented brief §6 faithfully in `tools/fake_mock.py`:
- the protocol and CRC rule;
- the 10 s buffer;
- ramp and signal modes;
- every listed fault: stalls, corrupt replies, duplicate replies, sensor dropout and restart.

`tools/verify_export.py` serves as the ground-truth check. The pipeline uses only the documented protocol, so it runs unchanged against the official mock, which is never modified.

## Assumptions (where the brief is open)

1. The "≥ 100 samples after the previous peak" chain carries across the session.
2. Per-channel statistics are output as `mean_1/min_1/max_1/mean_2/min_2/max_2` lists; `null` when there are no samples.
3. Rounding is half away from zero, to 1 decimal place.
4. Windows processed offline keep `mode: offline` and are not recomputed online.

## AI disclosure

In line with the assessment rules, I disclose that I used **Claude (Anthropic), through Claude
Code**, as an AI pair-programming assistant during this assessment.

- **How it was used:** to discuss and refine the architecture, draft and refactor code, write
  unit and failure tests, and prepare documentation (README, design note, evidence summary).
- **What I did:** I set the requirements and priorities, made the design trade-offs (for example
  free-tier Render + Neon instead of a VM, a 200 ms commit interval for a 0.5 s power-cut bound,
  and an ARM64 container as the Raspberry Pi stand-in), ran every test on the ARM64 environment
  and the deployed cloud, investigated the failures they exposed, and reviewed the results
  before submission.
- **Verification:** every figure reported here comes from tests that were actually run. Logs
  and summaries are in the evidence folder and can be reproduced with the scripts in `tests/`.
