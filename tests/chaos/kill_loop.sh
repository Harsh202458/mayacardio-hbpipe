#!/bin/sh
# T2: SIGKILL the pipeline N times at random 30-120 s intervals; systemd restarts it.
. "$(dirname "$0")/common.sh"
N="${1:-20}"
begin kill_loop
for i in $(seq 1 "$N"); do
  sleep $(( 30 + $(od -An -N1 -tu1 /dev/urandom) % 90 ))
  PID=$(systemctl show -p MainPID --value hbpipe)
  kill -9 "$PID"
  log "kill -9 $PID ($i/$N)"
  sleep 2
  log "restarted as $(systemctl show -p MainPID --value hbpipe)"
done
log "backlog drained after $(wait_backlog_zero) s"
verify_exports
log END
