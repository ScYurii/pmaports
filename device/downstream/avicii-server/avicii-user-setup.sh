#!/bin/sh
# Set up the default user once: bash as login shell (for completion) and
# the docker group. pmbootstrap creates the user (always UID 10000) after
# the packages are installed, so post-install cannot do this on a fresh
# install; the avicii-user-setup boot service runs it on the first boot.
marker=/var/lib/avicii-server/user-setup-done
[ -e "$marker" ] && exit 0

u=$(getent passwd 10000 | cut -d: -f1)
[ -n "$u" ] || exit 0

# Only replace the default shell, keep a shell the owner chose
if getent passwd "$u" | grep -q ':/bin/ash$'; then
	usermod -s /bin/bash "$u" || exit 1
fi
if getent group docker >/dev/null && ! id -nG "$u" | grep -qw docker; then
	addgroup "$u" docker || exit 1
fi

mkdir -p "${marker%/*}" && touch "$marker"
