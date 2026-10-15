# Demo helpers for the video (PowerShell). Load with:
#   Set-ExecutionPolicy -Scope Process Bypass -Force; . "$HOME\.hbpipe\demo.ps1"
# Uses the ARM64 device image and the deployed cloud (env file with HB_CLOUD_URL / HB_CLOUD_TOKEN).
# Note: commands passed to `sh -c` contain no double quotes (Windows PowerShell 5.1 mangles them).

$EnvFile = "$env:USERPROFILE\.hbpipe\device.env"
$Dev = "hb-demo"

function Start-Device {
    docker rm -f $Dev 2>$null | Out-Null
    docker volume rm -f hb-demo-data 2>$null | Out-Null
    docker run -d --name $Dev --platform linux/arm64 --cpus 4 --memory 2g -v hb-demo-data:/data `
        --env-file $EnvFile -e HB_DEVICE_ID=arm64-demo -e MOCK_MODE=signal -e MOCK_DURATION=3600 hbpipe-device | Out-Null
    "Device started (ARM64, 4 CPUs, 2 GB). Wait ~40 s for it to come online."
}

function Show-Device {
    docker exec $Dev sh -c 'echo arch: $(uname -m)   cpus: $(nproc)   memory_limit_MB: $(( $(cat /sys/fs/cgroup/memory.max) / 1048576 ))'
}

# Run in its own window: live status every second.
function Watch-Status {
    while ($true) {
        $s = docker exec $Dev pipeline status 2>$null
        Clear-Host
        "{0}   {1}" -f (Get-Date -Format "HH:mm:ss"), $s
        Start-Sleep -Milliseconds 700
    }
}

function Cut-Network     { docker network disconnect bridge $Dev; "NETWORK CUT at $(Get-Date -Format HH:mm:ss)" }
function Restore-Network { docker network connect bridge $Dev;    "NETWORK RESTORED at $(Get-Date -Format HH:mm:ss)" }

function Show-Offline-Results {
    docker exec $Dev sh -c 'pipeline results --session latest --from device | tail -2'
}

function Kill-Pipeline {
    docker exec $Dev sh -c 'pkill -9 -f hbpipe.device.cli'
    "kill -9 sent at $(Get-Date -Format HH:mm:ss): supervisor restarts it"
}

function Power-Cut {
    docker kill $Dev | Out-Null
    "POWER CUT at $(Get-Date -Format HH:mm:ss) (device and mock killed)"
    Start-Sleep 3
    docker start $Dev | Out-Null
    "Power restored at $(Get-Date -Format HH:mm:ss)"
}

function Show-Sessions { docker exec $Dev pipeline sessions }

# Proof: device export vs cloud export vs the known signal, for the session before the power cut.
function Verify-Session([string]$Session) {
    $cmd = 'cd /opt/hbpipe && pipeline export --session SID --from device > /tmp/d.jsonl && pipeline export --session SID --from cloud > /tmp/c.jsonl && python3 tools/verify_export.py /tmp/d.jsonl --mode signal --compare /tmp/c.jsonl | grep -e samples -e wrong_values -e lost_samples -e silent_holes -e identical -e loss_records -e PASS'
    docker exec $Dev sh -c $cmd.Replace('SID', $Session)
}

"Loaded: Start-Device, Show-Device, Watch-Status, Cut-Network, Restore-Network, Show-Offline-Results, Kill-Pipeline, Power-Cut, Show-Sessions, Verify-Session <id>"
