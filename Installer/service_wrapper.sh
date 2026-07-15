#!/bin/bash
# E3DC-Control V4 Service Wrapper
# Erlaubt sichere Steuerung klar definierter E3DC-Dienste durch die WebUI.
# Keine Wildcards, keine Legacy-C++-Unit, keine freien systemctl-Ziele.

set -u

ACTION=${1:-}
SERVICE=${2:-}

if [ -z "$ACTION" ] || [ -z "$SERVICE" ]; then
    echo "Usage: $0 <start|stop|restart|status|enable|disable> <service>"
    exit 1
fi

if [[ ! "$ACTION" =~ ^(start|stop|restart|status|enable|disable)$ ]]; then
    echo "Unzulaessige Aktion: $ACTION"
    exit 1
fi

# Dienstname normalisieren, aber keine Pfade oder Shell-Fragmente akzeptieren.
if [[ "$SERVICE" == *"/"* || "$SERVICE" == *";"* || "$SERVICE" == *" "* || "$SERVICE" == *"&"* || "$SERVICE" == *"|"* ]]; then
    echo "Unzulaessiger Dienst: $SERVICE"
    exit 1
fi

if [[ "$SERVICE" != *.service ]]; then
    SERVICE="${SERVICE}.service"
fi

# Exakte Allowlist. Der alte C++-Dienst e3dc.service bleibt absichtlich
# draussen und wird nur vom Update-/Cleanup-Pfad gestoppt.
ALLOWED_SERVICES=(
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
    echo "Dienst '$SERVICE' ist nicht fuer die Ausfuehrung ueber den Web-Wrapper freigegeben."
    exit 1
fi

echo "Fuehre systemctl $ACTION $SERVICE aus..."
if [ "$ACTION" == "status" ]; then
    systemctl status "$SERVICE" --no-pager
else
    systemctl "$ACTION" "$SERVICE"
fi
exit $?
