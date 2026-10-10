#!/bin/sh
# msm-firmware-loader in sysinit does not work on this device (it mounts its
# tmpfs before creating the directory) and the symlinked firmware was missing
# for early drivers: venus failed to load, v4l_id hung and the SoC escalated
# that into reboots every ~10 minutes. So copy the blobs once into the
# kernel's firmware path instead. Drivers that probed before the copy only
# get their firmware after the next reboot.

DEST=/lib/firmware/postmarketos
MARK="$DEST/.avicii-firmware"
BASE=/run/msm-firmware-loader

[ -e "$MARK" ] && exit 0

# Copied by hand before this package existed
if [ "$(find "$DEST" -type f 2>/dev/null | wc -l)" -gt 100 ]; then
	touch "$MARK"
	exit 0
fi

mkdir -p "$BASE"
/usr/sbin/msm-firmware-loader.sh || exit 1

# The loader may link the target dir into itself as the devicetree firmware
# prefix; drop such links, cp -L would recurse into them forever
find "$BASE/target" -type l | while read -r l; do
	[ "$(readlink "$l")" = "$BASE/target" ] && rm "$l"
done

mkdir -p "$DEST"
cp -rL "$BASE/target/." "$DEST/" || exit 1
printf "%s" "$DEST" > /sys/module/firmware_class/parameters/path

for m in "$BASE"/mnt/*; do
	umount "$m" 2>/dev/null
done
umount "$BASE" 2>/dev/null

n=$(find "$DEST" -type f | wc -l)
if [ "$n" -gt 100 ]; then
	touch "$MARK"
	echo "avicii-firmware: copied $n files to $DEST, reboot once to load them in all drivers"
else
	echo "avicii-firmware: only $n files found, will retry on next boot" >&2
	exit 1
fi
