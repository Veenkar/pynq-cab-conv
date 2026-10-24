#!/bin/bash
# Re-enable the on-board Ethernet (undo eth-disable.sh), persistently: removes the
# bootargs= line from /boot/uEnv.txt (the file is deleted if nothing else is left),
# so the kernel uses the device tree's default command line again.
#   sudo ~/eth-enable.sh     (takes effect at next reboot)
set -e
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
ENV=/boot/uEnv.txt

if [ -f "$ENV" ]; then
    grep -v '^bootargs=' "$ENV" > "$ENV.tmp" || true
    if grep -q '[^[:space:]]' "$ENV.tmp"; then
        mv "$ENV.tmp" "$ENV"
    else
        rm -f "$ENV.tmp" "$ENV"
    fi
    sync
fi
echo "Ethernet enabled from next boot"
