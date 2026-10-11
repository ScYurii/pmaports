#!/bin/sh
# Inbound firewall for the phone server, with iptables-legacy: nf_tables in the
# 4.19 kernel is too old for nftables, and Docker on this phone uses the legacy
# tables too.
#
# - USB (usb0) is a cable to the PC: everything is allowed, it is the way back in.
# - From the LAN only the ports in $TCP_PORTS; IPv6 only from link-local addresses
#   (the phone has no global IPv6 address on Wi-Fi).
# - Ports published by Docker are forwarded, not delivered to INPUT: they are
#   limited to the LAN in the DOCKER-USER chain.
#
# Usage: avicii-firewall start|stop
# Settings in /etc/conf.d/avicii-firewall: LAN, TCP_PORTS, USB_IF, WAN_IF.

LAN=${LAN:-192.168.1.0/24}
TCP_PORTS=${TCP_PORTS:-22 8080 8081}
USB_IF=${USB_IF:-usb0}
WAN_IF=${WAN_IF:-wlan0}
IPT=iptables-legacy
IP6T=ip6tables-legacy
CHAIN=AVICII-IN

ports() { echo "$TCP_PORTS" | tr ' ' ','; }

chain() {
	# Create or empty our chain and make INPUT jump to it once
	$1 -N $CHAIN 2>/dev/null || $1 -F $CHAIN
	$1 -C INPUT -j $CHAIN 2>/dev/null || $1 -I INPUT 1 -j $CHAIN
}

start() (
	# Stop at the first failed rule: the closing DROP is added last, so a rule
	# that the kernel rejects never leaves the phone half-closed
	set -e
	chain $IPT
	$IPT -A $CHAIN -i lo -j ACCEPT
	$IPT -A $CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
	$IPT -A $CHAIN -m conntrack --ctstate INVALID -j DROP
	$IPT -A $CHAIN -i "$USB_IF" -j ACCEPT
	$IPT -A $CHAIN -p icmp -j ACCEPT
	$IPT -A $CHAIN -s "$LAN" -p tcp -m multiport --dports "$(ports)" -j ACCEPT
	$IPT -A $CHAIN -j DROP

	# Docker-published ports: new connections from Wi-Fi only from the LAN.
	# Docker creates DOCKER-USER itself and never puts rules into it.
	$IPT -N DOCKER-USER 2>/dev/null || $IPT -F DOCKER-USER
	$IPT -A DOCKER-USER -i "$WAN_IF" -m conntrack --ctstate NEW ! -s "$LAN" -j DROP
	$IPT -A DOCKER-USER -j RETURN

	chain $IP6T
	$IP6T -A $CHAIN -i lo -j ACCEPT
	$IP6T -A $CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
	$IP6T -A $CHAIN -m conntrack --ctstate INVALID -j DROP
	$IP6T -A $CHAIN -p ipv6-icmp -j ACCEPT       # neighbour discovery needs it
	$IP6T -A $CHAIN -i "$USB_IF" -j ACCEPT
	$IP6T -A $CHAIN -s fe80::/10 -p tcp -m multiport --dports "$(ports)" -j ACCEPT
	$IP6T -A $CHAIN -j DROP
)

stop() {
	for t in $IPT $IP6T; do
		$t -D INPUT -j $CHAIN 2>/dev/null
		$t -F $CHAIN 2>/dev/null
		$t -X $CHAIN 2>/dev/null
	done
	$IPT -F DOCKER-USER 2>/dev/null
	$IPT -A DOCKER-USER -j RETURN 2>/dev/null
}

case "$1" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: $0 start|stop" >&2; exit 1 ;;
esac
