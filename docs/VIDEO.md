# Demo video script (≤ 5 minutes)

**Record:** Windows Game Bar (`Win + Alt + R` to start/stop), or OBS (free).
**Layout:** two PowerShell windows side by side.
- **Left:** live status, refreshing every second.
- **Right:** where you type the commands.

Increase the font size first (Ctrl + mouse wheel).

## Before recording (not filmed)
```powershell
# 1. Wake the cloud (open in a browser or:)
curl.exe https://<your-service>.onrender.com/health
# 2. Start the ARM64 device (fresh data volume, 1 h ramp session)
docker rm -f hb-dev; docker volume rm hb-data
docker run -d --name hb-dev --platform linux/arm64 --cpus 4 --memory 2g `
  -v hb-data:/data -e HB_CLOUD_URL=https://<your-service>.onrender.com -e HB_CLOUD_TOKEN=<token> `
  -e MOCK_DURATION=3600 hbpipe-device
# 3. Left window: live status
while ($true) { Clear-Host; Get-Date -Format "HH:mm:ss"; docker exec hb-dev pipeline status; Start-Sleep 1 }
```
Wait ~30 s until the left window shows `"mode": "online"` and a backlog near 0.

## Script

| Time | Say / do |
|---|---|
| **0:00–0:40** | "This is my pipeline for 'Never Lose a Heartbeat'. The device is an ARM64 Debian container limited to 4 CPUs and 2 GB, running the sensor mock and the pipeline. The cloud is a free Render service with Neon Postgres." Run `docker exec hb-dev sh -c "uname -m; nproc; cat /sys/fs/cgroup/memory.max"` → `aarch64`, `4`, `2147483648`. Point at the status: online, samples saved, samples in cloud, backlog. |
| **0:40–1:50** | **Network cut.** "I cut the device's network." `docker network disconnect bridge hb-dev`. Count aloud. Mode flips to **offline** in ~5 s; samples keep being saved; the backlog grows. Show `docker exec hb-dev pipeline results --session latest --from device \| Select-Object -Last 2` → `"mode": "offline"`. Restore with `docker network connect bridge hb-dev`. Mode returns to **online** in ~2 s and the backlog drains to 0. |
| **1:50–2:40** | **Process kill.** "Now I kill the pipeline process with SIGKILL." `docker exec hb-dev pkill -9 -f "hbpipe.device.cli run"`. The status call fails for ~1 s; the supervisor (systemd on a real Pi) restarts it and the counts continue from where they were. "Restart resumes from the last committed index; the mock keeps 10 s of data, so nothing is lost." |
| **2:40–3:50** | **Power cut.** "Now a power cut: I kill the whole device, mock included." `docker kill hb-dev`, wait 3 s, `docker start hb-dev`. A new mock session starts. Show the honest record of the cut: `docker exec hb-dev sh -c "pipeline sessions; pipeline export --session <old-id> --from device \| tail -2"` → the old session's tail loss with `"to_index": null, "reason": "device_power_loss_or_reboot"`. "At most half a second is lost on a power cut, and it is listed, never silent." |
| **3:50–4:40** | **Proof.** Stop the old-session check: `docker exec hb-dev pipeline export --session <old-id> --from device > dev.jsonl`, the same with `--from cloud > cloud.jsonl`, then `python tools/verify_export.py dev.jsonl --compare cloud.jsonl`. It shows `PASS`: every sample exactly once, unchanged, every loss listed, device == cloud. |
| **4:40–5:00** | "Results are computed by the same code online and offline. The full failure-test evidence (kills, network cuts, power cuts, disk full, clock changes, 2-hour catch-up, resource figures) is in the repository." |

Keep it under 5:00. Do one dry run first.
