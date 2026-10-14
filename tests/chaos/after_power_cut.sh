#!/bin/sh
# Run after the board comes back from a power cut (see power_cut.md).
. "$(dirname "$0")/common.sh"
begin power_cut
until systemctl is-active -q hbpipe; do sleep 1; done
sleep 20; snap
grep -E '"session_closed"|"start"' /var/lib/hbpipe/events.jsonl | tail -5 | tee -a "$OUT/log.txt"
verify_exports
log "tail losses:"; grep -h '"to_index": null' "$OUT"/device-*.jsonl | tee -a "$OUT/log.txt"
log END
