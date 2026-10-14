#!/bin/sh
# T8: jump the system clock forward/backward during a run (NTP paused meanwhile).
. "$(dirname "$0")/common.sh"
begin clock_skew
timedatectl set-ntp false
for jump in "+1 hour" "-2 hours" "+1 day" "-1 day"; do
  date -s "$jump" >/dev/null; log "clock jumped $jump -> $(date -u)"; sleep 30; snap
done
timedatectl set-ntp true
log "backlog drained after $(wait_backlog_zero) s"
verify_exports
log END
