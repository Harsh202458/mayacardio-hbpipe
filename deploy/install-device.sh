#!/bin/sh
# Install the device pipeline on a fresh Raspberry Pi OS (64-bit) / Debian / Ubuntu ARM64 board.
#   sudo ./deploy/install-device.sh https://your-cloud-host  YOUR_TOKEN
set -eu
CLOUD_URL="${1:?usage: install-device.sh CLOUD_URL TOKEN}"
TOKEN="${2:?usage: install-device.sh CLOUD_URL TOKEN}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
python3 - <<'PY'
import sqlite3, sys
assert sys.version_info >= (3, 9), "Python 3.9+ required"
assert sqlite3.sqlite_version_info >= (3, 24), "SQLite 3.24+ required"
PY

id hbpipe >/dev/null 2>&1 || useradd --system --home /var/lib/hbpipe --shell /usr/sbin/nologin hbpipe
install -d -o hbpipe -g hbpipe -m 750 /var/lib/hbpipe
install -d -m 755 /opt/hbpipe /etc/hbpipe
rm -rf /opt/hbpipe/hbpipe && cp -r "$SRC/hbpipe" /opt/hbpipe/
python3 -m compileall -q /opt/hbpipe/hbpipe   # bytecode ready: faster restart after a crash
install -m 755 "$SRC/bin/pipeline" /usr/local/bin/pipeline

if [ ! -f /etc/hbpipe/pipeline.env ]; then
  cat > /etc/hbpipe/pipeline.env <<EOF
HB_MOCK_HOST=127.0.0.1
HB_MOCK_PORT=7100
HB_DATA_DIR=/var/lib/hbpipe
HB_CLOUD_URL=$CLOUD_URL
HB_CLOUD_TOKEN=$TOKEN
HB_DEVICE_ID=$(hostname)
EOF
  chmod 640 /etc/hbpipe/pipeline.env
  chgrp hbpipe /etc/hbpipe/pipeline.env
fi

install -m 644 "$SRC/deploy/hbpipe.service" /etc/systemd/system/hbpipe.service
systemctl daemon-reload
systemctl enable --now hbpipe.service
echo "installed. check:  pipeline status   |   journalctl -u hbpipe -f"
