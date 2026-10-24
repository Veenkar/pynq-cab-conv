#!/bin/bash
# Disable the on-board Ethernet (macb) at boot, persistently. Saves ~38 s of boot:
# the macb probe stalls in the MDIO PHY scan. Wi-Fi is unaffected.
# Works by adding initcall_blacklist=macb_driver_init to the kernel command line via
# bootargs= in /boot/uEnv.txt (U-Boot's boot.scr imports it). Undo: eth-enable.sh.
# If the board ever fails to boot, delete uEnv.txt from the SD card's FAT partition.
#   sudo ~/eth-disable.sh    (takes effect at next reboot)
set -e
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
ENV=/boot/uEnv.txt
BL=initcall_blacklist=macb_driver_init

# current command line without any earlier blacklist entry
args=$(sed -e "s/ *$BL//g" /proc/cmdline)
if [ -f "$ENV" ]; then
    grep -v '^bootargs=' "$ENV" > "$ENV.tmp" || true
else
    : > "$ENV.tmp"
fi
echo "bootargs=$args $BL" >> "$ENV.tmp"
mv "$ENV.tmp" "$ENV"
sync
echo "Ethernet disabled from next boot ($ENV):"
cat "$ENV"
