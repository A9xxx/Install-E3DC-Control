#!/bin/bash
# E3DC-Control V4 Service Launcher
# Erlaubt sichere Steuerung klar definierter E3DC-Dienste durch die WebUI.
# Keine Wildcards, keine Legacy-C++-Unit, keine freien systemctl-Ziele.

set -euo pipefail
umask 022

readonly ENV="/usr/bin/env"
readonly SYSTEMCTL="/usr/bin/systemctl"

if (( EUID != 0 )); then
    printf 'Der Service-Launcher darf nur mit Root-Rechten ausgeführt werden.\n' >&2
    exit 77
fi

if (( $# != 2 )); then
    printf 'Aufruf: %s <start|stop|restart|status|enable|disable> <service>\n' "$0" >&2
    exit 64
fi

readonly ACTION="$1"
REQUESTED_SERVICE="$2"

case "$ACTION" in
    start|stop|restart|status|enable|disable)
        ;;
    *)
        printf 'Unzulässige Aktion: %s\n' "$ACTION" >&2
        exit 64
        ;;
esac

if [[ ! "$REQUESTED_SERVICE" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
    printf 'Unzulässiger Dienst: %s\n' "$REQUESTED_SERVICE" >&2
    exit 64
fi

if [[ "$REQUESTED_SERVICE" != *.service ]]; then
    REQUESTED_SERVICE="${REQUESTED_SERVICE}.service"
fi
readonly SERVICE="$REQUESTED_SERVICE"

# Exakte Allowlist. Der alte C++-Dienst e3dc.service bleibt absichtlich
# draußen und wird nur vom Update-/Cleanup-Pfad gestoppt.
readonly -a ALLOWED_SERVICES=(
    "e3dc-live.service"
    "energy_manager.service"
    "e3dc-wallbox-manager.service"
    "e3dc-epex-manager.service"
    "e3dc-weather-manager.service"
    "e3dc-storage-simulator.service"
    "e3dc-storage-manager.service"
    "e3dc-ha.service"
    "e3dc-matter-bridge.service"
    "e3dc-bluelink.service"
    "e3dc-lux-live.service"
    "e3dc-idm-live.service"
    "e3dc-stiebel-live.service"
    "e3dc-dimplex-live.service"
    "e3dc-heizstab.service"
    "e3dc-climate-live.service"
    "e3dc-climate-control.service"
    "e3dc-forecast-evidence.service"
    "e3dc-notifier.service"
    "e3dc-mqtt-hub.service"
    "e3dc-websocket.service"
    "e3dc-shadow-sync.service"
)

SERVICE_OK=0
for S in "${ALLOWED_SERVICES[@]}"; do
    if [ "$SERVICE" == "$S" ]; then
        SERVICE_OK=1
        break
    fi
done

if [ $SERVICE_OK -eq 0 ]; then
    printf "Dienst '%s' ist nicht für die Ausführung über den Web-Launcher freigegeben.\n" "$SERVICE" >&2
    exit 64
fi

if [[ ! -x "$ENV" || ! -x "$SYSTEMCTL" ]]; then
    printf 'Fest gebundene Systemprogramme sind nicht ausführbar.\n' >&2
    exit 126
fi

printf 'Führe systemctl %s %s aus...\n' "$ACTION" "$SERVICE"
if [ "$ACTION" == "status" ]; then
    exec "$ENV" -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C \
        "$SYSTEMCTL" --no-pager status -- "$SERVICE"
fi
exec "$ENV" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    "$SYSTEMCTL" "$ACTION" -- "$SERVICE"
