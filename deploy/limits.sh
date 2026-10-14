#!/bin/sh
# Record the device environment and its resource limits as evidence (criterion 8).
set -eu
OUT="${1:-evidence/env.txt}"
mkdir -p "$(dirname "$OUT")"
{
  echo "date: $(date -u +%FT%TZ)"
  echo "arch: $(uname -m)"; uname -a
  echo "cpus visible: $(nproc)"; free -m
  echo "--- hbpipe.service limits"
  systemctl show hbpipe -p CPUQuotaPerSecUSec -p MemoryMax -p Restart -p RestartUSec
  echo "--- os"; cat /etc/os-release
  echo "--- python / sqlite"; python3 -c 'import sys,sqlite3;print(sys.version);print(sqlite3.sqlite_version)'
} | tee "$OUT"
