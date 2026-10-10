#!/bin/sh
# Потребление телефона в реальном времени: avicii-power [интервал_с]
# Система = вход USB + батарея (разряд со знаком «+», заряд — «−»).
# При отключённом входе (online=0) датчик USB показывает мусор — тогда он не учитывается.

INTERVAL=${1:-1}
B=/sys/class/power_supply/battery
U=/sys/class/power_supply/usb

# Ширину полей с кириллицей задаём вручную: printf в sh и busybox awk считают байты, а не символы
echo "время     режим       система            |  вход USB           |  батарея              заряд"
while :; do
	online=$(cat $U/online)
	uv=$(cat $U/voltage_now); ui=$(cat $U/input_current_now)
	bv=$(cat $B/voltage_now); bi=$(cat $B/current_now)
	cap=$(cat $B/capacity)
	awk -v t="$(date +%T)" -v on="$online" -v uv="$uv" -v ui="$ui" \
	    -v bv="$bv" -v bi="$bi" -v cap="$cap" 'BEGIN {
		if (on != 1) { uv = 0; ui = 0 }
		pu = uv / 1e6 * ui / 1e6          # Вт с USB
		pb = bv / 1e6 * bi / 1e3          # Вт от батареи (+ разряд, − заряд)
		ps = pu + pb
		is = ps / (bv / 1e6) * 1e3        # ток системы, приведённый к напряжению батареи
		if (on != 1)       mode = "батарея   "
		else if (bi < -20) mode = "сеть+заряд"
		else               mode = "сеть      "
		printf "%-8s  %s  %7.2fВт %5dмА  |  %7.2fВт %5dмА  |  %+7.2fВт %+5dмА  %3d%%\n",
			t, mode, ps, is, pu, ui / 1e3, pb, bi, cap
	}'
	sleep "$INTERVAL"
done
