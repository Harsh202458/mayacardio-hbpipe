#!/bin/sh
# Shared helpers for the failure tests. Run on the ARM64 device as root, with the real
# biosignal-mock and the installed hbpipe.service.
set -eu
EVID="${EVID:-evidence}"
CLOUD_HOST="${CLOUD_HOST:?set CLOUD_HOST to the cloud hostname or IP}"
# Adjust to the real tool's usage once known, e.g. "check_ground_truth --seed 7 --mode ramp"
CHECK="${CHECK:-check_ground_truth}"

ts() { date -u +%FT%T.%3NZ; }
log() { echo "[$(ts)] $*" | tee -a "$OUT/log.txt"; }
begin() { OUT="$EVID/$1/$(date -u +%Y%m%dT%H%M%S)"; mkdir -p "$OUT"; log "BEGIN $1"; }
snap() { pipeline status | tee -a "$OUT/status.jsonl"; }
mode_now() { pipeline status | python3 -c 'import json,sys;print(json.load(sys.stdin)["mode"])'; }

# Seconds until `pipeline status` reports mode $1.
wait_mode() {
  start=$(date +%s%N)
  while [ "$(mode_now)" != "$1" ]; do sleep 0.2; done
  ms=$(( ($(date +%s%N) - start) / 1000000 ))
  printf '%d.%03d' $((ms / 1000)) $((ms % 1000))
}

backlog_now() { pipeline status | python3 -c 'import json,sys;print(json.load(sys.stdin)["backlog"])'; }

wait_backlog_zero() {
  start=$(date +%s)
  while [ "$(backlog_now)" != "0" ]; do sleep 1; done
  echo $(( $(date +%s) - start ))
}

verify_exports() {
  for sid in $(pipeline sessions | python3 -c 'import json,sys;[print(json.loads(l)["session"]) for l in sys.stdin]'); do
    pipeline export --session "$sid" --from device > "$OUT/device-$sid.jsonl"
    pipeline export --session "$sid" --from cloud  > "$OUT/cloud-$sid.jsonl"
    log "check device $sid"; $CHECK "$OUT/device-$sid.jsonl" 2>&1 | tee -a "$OUT/check.txt"
    log "check cloud  $sid"; $CHECK "$OUT/cloud-$sid.jsonl"  2>&1 | tee -a "$OUT/check.txt"
    if cmp -s "$OUT/device-$sid.jsonl" "$OUT/cloud-$sid.jsonl"; then log "device == cloud: yes"
    else log "device == cloud: NO"; fi
  done
  cp /var/lib/hbpipe/events.jsonl "$OUT/" 2>/dev/null || true
}
