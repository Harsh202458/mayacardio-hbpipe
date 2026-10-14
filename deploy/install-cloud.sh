#!/bin/sh
# Install the cloud API on a free-tier Ubuntu/Debian VM.
#   sudo ./deploy/install-cloud.sh YOUR_HOSTNAME YOUR_TOKEN
set -eu
HOST="${1:?usage: install-cloud.sh HOSTNAME TOKEN}"
TOKEN="${2:?usage: install-cloud.sh HOSTNAME TOKEN}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update && apt-get install -y python3 caddy
id hbcloud >/dev/null 2>&1 || useradd --system --home /var/lib/hbcloud --shell /usr/sbin/nologin hbcloud
install -d -o hbcloud -g hbcloud -m 750 /var/lib/hbcloud
install -d -m 755 /opt/hbpipe /etc/hbcloud
rm -rf /opt/hbpipe/hbpipe && cp -r "$SRC/hbpipe" /opt/hbpipe/
printf 'HB_CLOUD_TOKEN=%s\n' "$TOKEN" > /etc/hbcloud/cloud.env
chmod 640 /etc/hbcloud/cloud.env && chgrp hbcloud /etc/hbcloud/cloud.env

install -m 644 "$SRC/deploy/hbcloud.service" /etc/systemd/system/hbcloud.service
sed "s/YOUR_HOSTNAME/$HOST/" "$SRC/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now hbcloud.service
systemctl restart caddy
echo "open ports 80/443 in the provider firewall; then: curl https://$HOST/health"
