#!/usr/bin/env bash
#
# setup_reachy_udev.sh — install correct udev rules for Reachy Mini USB access.
# Run with sudo:  sudo bash setup_reachy_udev.sh
#
# Grants 0666 on the Reachy motor serial board (1a86:55d3) and the audio/mic
# device (38fb:1001) so the daemon + libusb can open them without root. The
# audio rule fixes mic-array init + Direction-of-Arrival (Errno 13) and the
# silent microphone.

set -e
RULES=/etc/udev/rules.d/99-reachy-mini.rules

cat > "$RULES" <<'UDEV'
SUBSYSTEM=="usb", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", MODE="0666", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="38fb", ATTRS{idProduct}=="1001", MODE="0666", GROUP="dialout"
SUBSYSTEM=="usb", ATTRS{idVendor}=="38fb", ATTRS{idProduct}=="1002", MODE="0666", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", MODE="0666", GROUP="dialout"
UDEV

echo "Wrote $RULES:"
cat "$RULES"

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=38fb
udevadm trigger --subsystem-match=usb --attr-match=idVendor=1a86
udevadm trigger --subsystem-match=tty
udevadm settle || true

echo
echo "Resulting Reachy USB node permissions:"
for vp in 38fb:1001 38fb:1002 1a86:55d3; do
  line=$(lsusb -d "$vp" 2>/dev/null) || continue
  bus=$(echo "$line" | awk '{print $2}'); dev=$(echo "$line" | awk '{print $4}' | tr -d ':')
  [ -n "$bus" ] && ls -l "/dev/bus/usb/$bus/$dev" 2>/dev/null
done
echo
echo "If a device still shows 'root root', unplug and replug the robot's USB, then re-check."
