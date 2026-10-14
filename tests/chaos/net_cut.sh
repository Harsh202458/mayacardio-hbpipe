#!/bin/sh
# T5: cut the network to the cloud for N seconds (packets silently dropped), restore,
# measure both mode switches and the catch-up time.   usage: net_cut.sh SECONDS
. "$(dirname "$0")/common.sh"
SECS="${1:-60}"
begin "net_cut_${SECS}s"
snap
IP=$(getent ahostsv4 "$CLOUD_HOST" | awk 'NR==1{print $1}')
iptables -I OUTPUT -d "$IP" -j DROP
log "network CUT ($IP)"
log "offline after $(wait_mode offline) s (limit 10 s)"
sleep "$SECS"
snap
iptables -D OUTPUT -d "$IP" -j DROP
log "network RESTORED"
log "online after $(wait_mode online) s (limit 10 s)"
log "backlog drained after $(wait_backlog_zero) s (limit 900 s)"
snap
verify_exports
log END
