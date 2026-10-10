#!/bin/sh
# Держит заряд батареи в окне CHG_MIN..CHG_MAX, пока телефон работает от сети.
#   выше CHG_MAX   — питание от батареи (input_suspend=1), разряд до CHG_MAX
#   окно           — питание от сети, заряд батареи выключен (mmi_charging_enable=0)
#   ниже CHG_MIN   — обычная зарядка до CHG_MAX
# При выходе всегда возвращает штатную зарядку.

CHG_MAX=${CHG_MAX:-80}
CHG_MIN=${CHG_MIN:-70}
INTERVAL=${INTERVAL:-60}

BATT=/sys/class/power_supply/battery
SUSP=$BATT/input_suspend
MMI=/sys/class/oplus_chg/battery/mmi_charging_enable

log() { echo "$(date '+%F %T') $*"; }

put() { [ "$(cat "$1")" = "$2" ] || echo "$2" > "$1"; }

restore() {
	echo 0 > $SUSP
	echo 1 > $MMI
	log "stop: штатная зарядка восстановлена"
	exit 0
}
trap restore INT TERM

if [ "$CHG_MIN" -ge "$CHG_MAX" ]; then
	log "ошибка: CHG_MIN ($CHG_MIN) должен быть меньше CHG_MAX ($CHG_MAX)"
	exit 1
fi

log "start: окно $CHG_MIN..$CHG_MAX%"
state=
while :; do
	cap=$(cat $BATT/capacity)
	if [ "$cap" -gt "$CHG_MAX" ]; then
		new=drain
	elif [ "$cap" -le "$CHG_MIN" ]; then
		new=charge
	elif [ "$state" = charge ] && [ "$cap" -lt "$CHG_MAX" ]; then
		new=charge
	else
		new=hold
	fi

	case $new in
	drain)
		put $MMI 0
		put $SUSP 1
		# Драйвер OPLUS иногда снимает отключение входа напрямую (charger_unsuspend),
		# в обход голоса input_suspend: sysfs показывает 1, а питание идёт от сети.
		# Признак — батарея не разряжается. Тогда переключаем голос заново.
		if [ "$state" = drain ] && [ "$(cat $BATT/current_now)" -lt 50 ]; then
			echo 0 > $SUSP
			sleep 1
			echo 1 > $SUSP
			log "drain: вход снова включён драйвером, отключаю повторно"
		fi
		;;
	hold)   put $SUSP 0; put $MMI 0 ;;
	charge) put $SUSP 0; put $MMI 1 ;;
	esac

	if [ "$new" != "$state" ]; then
		log "$new: заряд $cap%, $(($(cat $BATT/temp) / 10))°C"
		state=$new
	fi
	sleep "$INTERVAL" & wait $!
done
