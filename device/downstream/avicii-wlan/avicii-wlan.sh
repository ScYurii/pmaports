#!/bin/sh
# Wi-Fi bring-up on OnePlus Nord (avicii), run by the avicii-wlan service.
# Keeps /dev/subsys_modem open (closing it releases the modem: the WLAN
# firmware runs inside it as wlan_pd) and runs wlfwd for the service lifetime.
LIB=/usr/lib/avicii-wlan
FW_DIR=/lib/firmware/postmarketos
STATE=/var/lib/wlfwd
BOARD="$STATE/board"
QCA="$FW_DIR/wlan/qca_cld"

log() { echo "$(date +%T) $*"; }

# Per-unit files from persist: OnePlus BDF/regdb (cnss-daemon on Android
# takes them from there; the copies in the modem partition crash the
# firmware), the driver ini and the factory MAC. Copied once, read-only.
copy_from_persist() {
	[ -s "$BOARD/bdwlan.bin" ] && [ -s "$BOARD/regdb.bin" ] &&
		[ -s "$QCA/WCNSS_qcom_cfg.ini" ] && [ -s "$QCA/wlan_mac.bin" ] &&
		return 0

	mnt=$(mktemp -d /run/avicii-persist.XXXXXX)
	if ! mount -o ro /dev/disk/by-partlabel/persist "$mnt"; then
		log "cannot mount persist"
		rmdir "$mnt"
		return 1
	fi
	mkdir -p "$BOARD" "$QCA"
	cp "$mnt/bdwlan.bin" "$mnt/regdb.bin" "$BOARD/" &&
		cp "$mnt/wlan/WCNSS_qcom_cfg.ini" "$mnt/wlan_mac.bin" "$QCA/"
	rc=$?
	umount "$mnt"
	rmdir "$mnt"
	[ $rc -eq 0 ] && log "copied board files from persist"
	return $rc
}

# "Intf0MacAddress=0011223344AB" -> 00:11:22:33:44:AB
read_mac() {
	sed -n 's/^Intf0MacAddress=\([0-9A-Fa-f]\{12\}\).*/\1/p' "$QCA/wlan_mac.bin" |
		sed 's/../&:/g; s/:$//'
}

copy_from_persist || exit 1
mac=$(read_mac)
if [ -z "$mac" ]; then
	log "no MAC in $QCA/wlan_mac.bin"
	exit 1
fi

grep -q " /sys/kernel/debug " /proc/mounts || mount -t debugfs none /sys/kernel/debug

# IPA only via /dev/ipa: opening /dev/subsys_ipa_fws directly panics in TZ
echo 1 > /dev/ipa
echo 1 > /sys/kernel/boot_adsp/boot
echo 1 > /sys/kernel/boot_cdsp/boot
exec 3< /dev/subsys_modem
log "modem opened"

# wlfwd follows WLFW through the QRTR name service and replays the
# bring-up after every WLAN PD restart
"$LIB/wlfwd" "$BOARD" "$mac" "$STATE" &
wlfwd_pid=$!

for i in $(seq 1 40); do
	grep -qE "FW_READY +[1-9]" /sys/kernel/debug/icnss/stats && break
	sleep 1
done
if grep -qE "FW_READY +[1-9]" /sys/kernel/debug/icnss/stats; then
	log "FW_READY after ${i}s"
else
	log "no FW_READY in 40s, trying ON anyway"
fi

# Exactly "ON"; the driver loads once per boot (hdd_loaded)
if echo ON > /dev/wlan; then
	log "wlan ON"
else
	log "wlan ON failed"
fi

wait "$wlfwd_pid"; rc=$?
log "wlfwd exited: $rc"
exit $rc
