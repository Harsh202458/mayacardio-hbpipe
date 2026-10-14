#!/bin/sh
# T7: run the data dir on a small loop filesystem, fill it, then free it.
#   Requires the service to be stopped first; restores the normal data dir afterwards.
. "$(dirname "$0")/common.sh"
begin disk_full
systemctl stop hbpipe
IMG=/var/tmp/hbpipe-small.img
truncate -s 96M "$IMG"; mkfs.ext4 -q -F "$IMG"
mv /var/lib/hbpipe /var/lib/hbpipe.real; mkdir /var/lib/hbpipe
mount -o loop "$IMG" /var/lib/hbpipe; chown hbpipe:hbpipe /var/lib/hbpipe
systemctl start hbpipe; sleep 30; snap
fallocate -l "$(( $(df --output=avail -B1 /var/lib/hbpipe | tail -1) - 1048576 ))" /var/lib/hbpipe/filler || true
log "disk filled"; sleep 60; snap
rm -f /var/lib/hbpipe/filler; log "space freed"; sleep 60; snap
grep -E '"disk_full"|"disk_recovered"' /var/lib/hbpipe/events.jsonl | tee -a "$OUT/log.txt"
verify_exports
systemctl stop hbpipe
cp -a /var/lib/hbpipe "$OUT/small-fs-copy"
umount /var/lib/hbpipe; rmdir /var/lib/hbpipe; mv /var/lib/hbpipe.real /var/lib/hbpipe
systemctl start hbpipe
log END
