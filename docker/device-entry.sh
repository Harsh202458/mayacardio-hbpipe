#!/bin/sh
# Container stand-in for systemd: starts the mock (unless HB_RUN_MOCK=0) and keeps the pipeline
# running with Restart=always / RestartSec=1 semantics. A new HB_BOOT_ID per container start lets
# the pipeline tell a "power cut" (container killed) from a mock restart.
HB_BOOT_ID=$(cat /proc/sys/kernel/random/uuid); export HB_BOOT_ID
mkdir -p /data/logs
if [ "${HB_RUN_MOCK:-1}" = "1" ]; then
  python3 /opt/hbpipe/tools/fake_mock.py --port "${HB_MOCK_PORT:-7100}" \
      --mode "${MOCK_MODE:-ramp}" --seed "${MOCK_SEED:-7}" --duration "${MOCK_DURATION:-3600}" \
      ${MOCK_ARGS:-} >> /data/logs/mock.log 2>&1 &
fi
while true; do
  pipeline run >> /data/logs/pipeline.log 2>&1
  echo "$(date -u +%FT%TZ) pipeline exited with $?, restarting in 1 s" >> /data/logs/supervisor.log
  sleep 1
done
