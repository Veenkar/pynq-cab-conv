#!/bin/bash
# Install / remove the boot-time start of the cab convolver on a PYNQ-Z2 (PYNQ 3.0).
# Run as root from the directory with cab-conv.service (e.g. ~xilinx/cab_conv):
#
#   sudo ./install_autostart.sh            install + enable cab-conv.service
#   sudo ./install_autostart.sh --fast     also speed up boot (IR ready ~85 s instead of ~160 s):
#                                          - /boot/boot.py: drop the base.bit load + LED blink
#                                            (original kept as /boot/boot.py.orig)
#                                          - disable jupyter.service and clear_pl_statefile.service
#                                            (the latter has Requires=jupyter.service)
#   sudo ./install_autostart.sh --restore  undo everything: disable cab-conv.service,
#                                          restore boot.py, re-enable Jupyter
set -e

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
UNIT=/etc/systemd/system/cab-conv.service

fast_boot() {
    [ -f /boot/boot.py.orig ] || cp /boot/boot.py /boot/boot.py.orig
    cat > /boot/boot.py <<'EOF'
#! /usr/bin/env python3
# Run at boot by bootpy.service. The default base overlay load + LED blink was
# removed (cab-conv.service loads its own overlay); original in boot.py.orig.

# example #1 - WiFi connection at boot
#from pynq.lib import Wifi
#
#port = Wifi()
#port.connect('your_ssid', 'your_password', auto=True)

# example #2 - Change hostname
#import subprocess
#subprocess.call('pynq_hostname.sh aNewHostName'.split())
EOF
    sync
    systemctl disable jupyter.service clear_pl_statefile.service
    echo "fast boot: boot.py trimmed (backup /boot/boot.py.orig), Jupyter disabled"
}

case "${1:-}" in
--restore)
    systemctl disable --now cab-conv.service 2>/dev/null || true
    rm -f "$UNIT"
    if [ -f /boot/boot.py.orig ]; then
        cp /boot/boot.py.orig /boot/boot.py && rm /boot/boot.py.orig && sync
    fi
    systemctl enable jupyter.service clear_pl_statefile.service
    systemctl daemon-reload
    echo "restored PYNQ defaults (takes effect at next boot)"
    ;;
""|--fast)
    install -m 644 "$HERE/cab-conv.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable cab-conv.service
    [ "${1:-}" = --fast ] && fast_boot
    echo "cab-conv.service enabled; edit ExecStart in $UNIT to change IR/levels"
    ;;
*)
    sed -n '2,13p' "$0"; exit 1
    ;;
esac
