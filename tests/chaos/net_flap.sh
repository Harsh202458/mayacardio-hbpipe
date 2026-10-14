#!/bin/sh
# T6: flapping network, 5 s down / 5 s up, 20 times.
. "$(dirname "$0")/common.sh"
begin net_flap
IP=$(getent ahostsv4 "$CLOUD_HOST" | awk 'NR==1{print $1}')
for i in $(seq 1 20); do
  iptables -I OUTPUT -d "$IP" -j DROP; sleep 5
  iptables -D OUTPUT -d "$IP" -j DROP; sleep 5
  snap >/dev/null
done
log "backlog drained after $(wait_backlog_zero) s"
verify_exports
log END
