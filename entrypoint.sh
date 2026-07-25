#!/bin/bash
set -e

# Wenn der Container mit einem gemounteten Repo laeuft, soll immer das
# aktuelle Startskript aus diesem Repo gewinnen. Der im Image gebackene
# Entrypoint ist nur noch ein Bootloader, damit neue Dienst-Startlogik
# nach `git pull` und Container-Neustart wirklich aktiv wird.
REPO_ENTRYPOINT="/app/pi/Install/entrypoint.sh"
if [ "${E3DC_ENTRYPOINT_DELEGATED:-0}" != "1" ] && [ -f "$REPO_ENTRYPOINT" ]; then
    CURRENT_ENTRYPOINT="$(readlink -f "$0" 2>/dev/null || echo "$0")"
    REPO_ENTRYPOINT_REAL="$(readlink -f "$REPO_ENTRYPOINT" 2>/dev/null || echo "$REPO_ENTRYPOINT")"
    if [ "$CURRENT_ENTRYPOINT" != "$REPO_ENTRYPOINT_REAL" ]; then
        export E3DC_ENTRYPOINT_DELEGATED=1
        chmod +x "$REPO_ENTRYPOINT" 2>/dev/null || true
        exec "$REPO_ENTRYPOINT" "$@"
    fi
fi

echo "=== Starte E3DC-Control Container (Production) ==="

RAMDISK_DIR="/var/www/html/ramdisk"
RAMDISK_CACHE_DIR="/var/www/html/data/docker_ramdisk_cache"
RAMDISK_CACHE_FILES="
pv_forecast.json
pv_forecast_history.json
weather_forecast.json
ml_prediction.json
storage_plan.json
epex_daten.json
awattar_cache.json
price_boost_plan.json
daily_peaks.json
live_history.txt
"

configure_apache_web_port() {
    local web_port="${E3DC_WEB_PORT:-80}"
    local web_bind="${E3DC_WEB_BIND:-}"
    local listen_target
    local vhost_target
    local bind_for_apache

    if ! echo "$web_port" | grep -Eq '^[0-9]+$' || [ "$web_port" -lt 1 ] || [ "$web_port" -gt 65535 ]; then
        echo "   -> WARNUNG: E3DC_WEB_PORT='$web_port' ist ungueltig, nutze Port 80."
        web_port="80"
    fi

    if [ -n "$web_bind" ]; then
        # Synology/Reverse-Proxy-Nutzer tragen hier teils IPv6-Adressen oder
        # versehentlich URLs ein. Apache erwartet fuer IPv6 [addr]:port.
        web_bind="$(printf '%s' "$web_bind" | sed -E 's#^https?://##; s#/.*$##')"
        web_bind="$(printf '%s' "$web_bind" | sed -E 's#^\[([^]]+)\]:[0-9]+$#\1#; s#^([0-9.]+):[0-9]+$#\1#')"
        if printf '%s' "$web_bind" | grep -q '^\[.*\]$'; then
            bind_for_apache="$web_bind"
        elif printf '%s' "$web_bind" | grep -q ':'; then
            bind_for_apache="[${web_bind}]"
        else
            bind_for_apache="$web_bind"
        fi
        listen_target="${bind_for_apache}:${web_port}"
        vhost_target="${bind_for_apache}:${web_port}"
    else
        listen_target="${web_port}"
        vhost_target="*:${web_port}"
    fi

    echo "-> Konfiguriere Apache Webserver auf ${listen_target}..."
    service apache2 stop >/dev/null 2>&1 || true
    cat > /etc/apache2/ports.conf <<EOF
Listen ${listen_target}
EOF
    sed -i -E "s|<VirtualHost [^>]+>|<VirtualHost ${vhost_target}>|" /etc/apache2/sites-available/000-default.conf
    if [ -d /etc/apache2/conf-enabled ]; then
        grep -RIl '^[[:space:]]*Listen[[:space:]]' /etc/apache2/conf-enabled /etc/apache2/sites-enabled 2>/dev/null \
            | while read -r apache_conf; do
                sed -i -E '/^[[:space:]]*Listen[[:space:]]/d' "$apache_conf"
            done
    fi
    echo "   -> Apache Listen-Konfiguration:"
    grep -R '^[[:space:]]*Listen[[:space:]]' /etc/apache2/ports.conf /etc/apache2/conf-enabled /etc/apache2/sites-enabled 2>/dev/null || true
    apache2ctl -t || true
}

restore_ramdisk_cache() {
    mkdir -p "$RAMDISK_DIR" "$RAMDISK_CACHE_DIR"
    local restored=0
    for f in $RAMDISK_CACHE_FILES; do
        if [ -f "$RAMDISK_CACHE_DIR/$f" ]; then
            cp -f "$RAMDISK_CACHE_DIR/$f" "$RAMDISK_DIR/$f" 2>/dev/null && restored=$((restored + 1)) || true
        fi
    done
    chown -R www-data:www-data "$RAMDISK_DIR" 2>/dev/null || true
    chmod 2775 "$RAMDISK_DIR" 2>/dev/null || true
    find "$RAMDISK_DIR" -maxdepth 1 -type f -exec chmod 664 {} \; 2>/dev/null || true
    if [ "$restored" -gt 0 ]; then
        echo "   -> Ramdisk-Warmstart: $restored Datei(en) aus data/docker_ramdisk_cache wiederhergestellt."
    fi
}

save_ramdisk_cache() {
    mkdir -p "$RAMDISK_CACHE_DIR"
    local saved=0
    for f in $RAMDISK_CACHE_FILES; do
        if [ -f "$RAMDISK_DIR/$f" ]; then
            cp -f "$RAMDISK_DIR/$f" "$RAMDISK_CACHE_DIR/$f" 2>/dev/null && saved=$((saved + 1)) || true
        fi
    done
    chown -R www-data:www-data "$RAMDISK_CACHE_DIR" 2>/dev/null || true
    chmod -R u+rwX,g+rwX,o-rwx "$RAMDISK_CACHE_DIR" 2>/dev/null || true
    if [ "$saved" -gt 0 ]; then
        echo "   -> Ramdisk-Warmstart: $saved Datei(en) nach data/docker_ramdisk_cache gesichert."
    fi
}

shutdown_container() {
    echo "-> Stop-Signal empfangen: sichere Ramdisk-Warmstartdaten..."
    save_ramdisk_cache || true
    pids="$(jobs -pr)"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        wait $pids 2>/dev/null || true
    fi
    exit 0
}
trap shutdown_container TERM INT

# Web-Interface aus dem gemounteten Repo in den Apache-Webroot synken.
# Der Code liegt via Volume unter /app/pi/Install/html/, Apache liest aus /var/www/html/.
echo "-> Synke Web-Interface (html/ -> /var/www/html/)..."
if [ -d "/app/pi/Install/html" ]; then
    if [ ! -d "/app/pi/Install/html/app" ]; then
        rm -rf /var/www/html/app
    fi
    cp -r /app/pi/Install/html/. /var/www/html/
    chown -R www-data:www-data /var/www/html/
    echo "   -> Web-Interface aktualisiert."
else
    echo "   -> WARNUNG: /app/pi/Install/html nicht gefunden! Repo korrekt gemountet?"
fi

# Matter-Abhängigkeiten einmalig im Container installieren.
MATTER_DIR="/app/pi/Install/Installer/matter"
if [ -f "$MATTER_DIR/package.json" ] && [ ! -f "$MATTER_DIR/package-lock.json" ]; then
    echo "-> FEHLER: Matter-Lockdatei fehlt; Start wird abgebrochen."
    exit 1
fi
if [ -f "$MATTER_DIR/package-lock.json" ] && [ ! -d "$MATTER_DIR/node_modules" ]; then
    echo "-> Installiere Matter-Abhängigkeiten..."
    cd "$MATTER_DIR" && npm ci --omit=dev --ignore-scripts && cd /app/pi/Install
fi

# Rechte des persistenten Daten-Ordners korrigieren
mkdir -p /var/www/html/data/matter-storage
chown -R www-data:www-data /var/www/html/data
chmod 2775 /var/www/html/data
find /var/www/html/data/matter-storage -type d -exec chmod 700 {} \;
find /var/www/html/data/matter-storage -type f -exec chmod 600 {} \;

# Config Management: Die Config lebt jetzt PERSISTENT im data-Ordner!
V4_CONFIG="/var/www/html/data/e3dc_v4.json"
CONFIG_FILE="/var/www/html/data/e3dc.config.txt"
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "/app/e3dc.config.txt" ]; then
        cp /app/e3dc.config.txt "$CONFIG_FILE"
    else
        touch "$CONFIG_FILE"
    fi
    chown www-data:www-data "$CONFIG_FILE"
fi

# Fehlende Hilfsdateien als leere Platzhalter anlegen, um Rechte-Probleme im UI zu vermeiden
for f in e3dc.strompreise.txt e3dc.wallbox.txt e3dc_v4.json; do
    if [ ! -f "/var/www/html/data/$f" ]; then
        if [ "$f" == "e3dc_v4.json" ]; then
            echo "{}" > "/var/www/html/data/$f"
        else
            touch "/var/www/html/data/$f"
        fi
        chown www-data:www-data "/var/www/html/data/$f"
        chmod 664 "/var/www/html/data/$f"
    fi
done

# Absicherung: Windows-Zeilenumbrüche (CRLF) entfernen, da sie das C++ Programm abstürzen lassen
sed -i 's/\r$//' "$CONFIG_FILE"

# Ramdisk sicherstellen: tmpfs ist gemountet (via docker-compose tmpfs:),
# aber Rechte und Basis-Verzeichnisstruktur muessen explizit gesetzt werden.
echo "-> Initialisiere Ramdisk (tmpfs)..."
mkdir -p "$RAMDISK_DIR"
chown -R www-data:www-data "$RAMDISK_DIR"
chmod 2775 "$RAMDISK_DIR"
restore_ramdisk_cache

# Logs-Ordner: www-data + Python-Prozesse muessen schreiben duerfen
mkdir -p /var/www/html/logs
chown -R www-data:www-data /var/www/html/logs
chmod -R 775 /var/www/html/logs

echo "-> Starte Apache Webserver..."
configure_apache_web_port
service apache2 start

echo "-> Starte D-Bus und Avahi für Matter mDNS..."
service dbus start >/dev/null 2>&1 || true
service avahi-daemon start >/dev/null 2>&1 || true

echo "-> Starte Python Hintergrunddienste..."
cd /app/pi/Install/Installer

# Nutze das sichere VENV aus dem Container
PYTHON_EXEC="/opt/venv/bin/python3"

# Führe V4-Migration aus, falls alte e3dc.config.txt existiert und Keys in e3dc_v4.json fehlen
echo "-> Prüfe und migriere alte Konfiguration..."
cd /app/pi/Install
$PYTHON_EXEC -c "from Installer.config_manager import run_config_wizard; run_config_wizard()"
cd /app/pi/Install/Installer

# Helper-Funktion zum Lesen aus V4 JSON
# $1 = key (z.B. wb_native_enable)
get_v4_val() {
    $PYTHON_EXEC -c "import sys, json; c=json.load(open('$V4_CONFIG')) if open('$V4_CONFIG').read().strip() else {}; print(c.get('$1', ''))" 2>/dev/null
}

# E3DC Live RSCP Client ZUERST starten -- liefert live_data_py.json fuer storage_simulator.
# Ohne diesen Schritt wuerde storage_simulator mit SOC=0% starten -> Discharge-Sperre!
echo "-> E3DC Live RSCP Client (zuerst, liefert Live-SoC)..."
nohup $PYTHON_EXEC e3dc_live.py --write --loops 0 --interval 3 > /proc/1/fd/1 2>&1 &
E3DC_LIVE_STARTED=1
echo "   -> Warte 10s auf erste RSCP-Messung..."
sleep 10

# === INITIALER SOFORT-FORECAST (VOR Simulator-Start!) ===
echo "-> Initialer PV-Forecast (--once, vor Daemon-Start)..."
if $PYTHON_EXEC Forecast/pv_forecast_service.py --once >> /var/www/html/logs/weather_manager.log 2>&1; then
    SLOTS=$(python3 -c "import json; d=json.load(open('/var/www/html/ramdisk/pv_forecast.json')); print(len(d))" 2>/dev/null || echo '?')
    echo "   -> pv_forecast.json erstellt (${SLOTS} Slots)."
else
    echo "   -> Forecast-Init fehlgeschlagen (API nicht erreichbar). Daemon holt nach."
fi

# ML-Vorhersage: Das anlagenspezifische Modell liegt in einem privaten Volume.
# Ein altes Web-Pickle wird niemals geladen oder übernommen.
export E3DC_ML_MODEL_DIR="/var/lib/e3dc-control/ml"
install -d -o root -g root -m 0700 "$E3DC_ML_MODEL_DIR"
if ! $PYTHON_EXEC ml_predictor.py --model-ready >/dev/null 2>&1; then
    echo "   -> Kein ML-Modell vorhanden: versuche einmaliges Training aus lokaler Historie..."
    $PYTHON_EXEC ml_predictor.py --train >> /var/www/html/logs/storage_simulator.log 2>&1 || true
fi
if $PYTHON_EXEC ml_predictor.py --model-ready >/dev/null 2>&1; then
    echo "   -> ML-Vorhersage (ml_predictor --predict)..."
    $PYTHON_EXEC ml_predictor.py --predict >> /var/www/html/logs/storage_simulator.log 2>&1 || true
else
    echo "   -> Kein ML-Modell vorhanden (< 50 Samples) -- Storage Simulator nutzt Fallback."
fi

# WebSocket Server (immer)
nohup $PYTHON_EXEC e3dc_websocket.py > /var/www/html/logs/e3dc_websocket.log 2>&1 &

# Klimaanlage Live (read-only)
# Der Worker prüft Aktivierung und Konfiguration in jedem Zyklus fail-closed.
echo "   -> Klimaanlagen-Monitor (read-only) aktiv."
nohup $PYTHON_EXEC climate_live.py > /var/www/html/logs/climate_live.log 2>&1 &

# Energy Manager (Lademanagement ODER Wärmepumpe)
AUTO_MODE=$(get_v4_val "auto_mode")
LUXTRONIK=$(get_v4_val "luxtronik")
IDM_IP=$(get_v4_val "idm_ip")
STIEBEL_ISG_IP=$(get_v4_val "stiebel_isg_ip")
WP_TYPE=$(get_v4_val "wp_type")
SHELLY_SG_IP=$(get_v4_val "shelly_sg_ip")
SHELLY_PAUSE_IP=$(get_v4_val "shelly_pause_ip")
HAS_SHELLY_SGREADY=0
if { [ -n "$SHELLY_SG_IP" ] && [ "$SHELLY_SG_IP" != "0.0.0.0" ]; } || { [ -n "$SHELLY_PAUSE_IP" ] && [ "$SHELLY_PAUSE_IP" != "0.0.0.0" ]; }; then
    HAS_SHELLY_SGREADY=1
fi

if [ "$AUTO_MODE" = "1" ] || [ "$LUXTRONIK" = "1" ] || [ -n "$IDM_IP" ] || [ "$HAS_SHELLY_SGREADY" = "1" ]; then
    echo "   -> Energy Manager aktiv."
    nohup $PYTHON_EXEC luxtronik/energy_manager.py > /proc/1/fd/1 2>&1 &
fi

# Luxtronik Live-Daten (NUR wenn Wärmepumpe explizit aktiviert)
if [ "$LUXTRONIK" = "1" ] && [ "$WP_TYPE" = "0" ]; then
    echo "   -> Luxtronik WebSocket-Client aktiv."
    nohup $PYTHON_EXEC luxtronik/lux_live.py > /var/www/html/logs/lux_live.log 2>&1 &
fi

# IDM Live-Daten (NUR wenn IDM-IP konfiguriert)
if [ "$WP_TYPE" = "1" ] && [ -n "$IDM_IP" ]; then
    echo "   -> IDM Modbus-Client aktiv."
    nohup $PYTHON_EXEC idm/idm_live.py > /var/www/html/logs/idm_live.log 2>&1 &
fi

# Stiebel ISG Live-Daten (read-only)
if [ "$WP_TYPE" = "4" ] && [ -n "$STIEBEL_ISG_IP" ] && [ "$STIEBEL_ISG_IP" != "0.0.0.0" ]; then
    echo "   -> Stiebel ISG Live aktiv."
    nohup $PYTHON_EXEC stiebel/stiebel_live.py > /var/www/html/logs/stiebel_live.log 2>&1 &
fi

# Heizstab Manager (wp_type=2: Modbus Heizstab/Shelly, wp_type=3: Shelly Pro3EM WP-Messung)
if [ "$WP_TYPE" = "2" ] || [ "$WP_TYPE" = "3" ]; then
    if [ "$WP_TYPE" = "3" ]; then
        echo "   -> Heizstab Manager (Shelly Pro3EM WP-Integration) aktiv."
    else
        echo "   -> Heizstab Manager (Shelly Heizstab/Modbus) aktiv."
    fi
    nohup $PYTHON_EXEC heizstab_manager.py > /var/www/html/logs/heizstab_manager.log 2>&1 &
fi

# Native Wallbox Manager (Python PID-Regler für Go-e etc.)
WB_NATIVE=$(get_v4_val "wb_native_enable")
if [ "$WB_NATIVE" = "1" ] || [ "$WB_NATIVE" = "true" ]; then
    echo "   -> Native Wallbox Manager aktiv."
    nohup $PYTHON_EXEC wallbox_manager.py > /proc/1/fd/1 2>&1 &
fi

# MQTT Hub (wenn in config konfiguriert)
MQTT_IP=$(get_v4_val "mqtt_hub_ip")
if [ -n "$MQTT_IP" ]; then
    echo "   -> MQTT Hub aktiv."
    nohup $PYTHON_EXEC e3dc_mqtt_hub.py > /proc/1/fd/1 2>&1 &
fi

# Bluelink (wenn konfiguriert)
BLUELINK=$(get_v4_val "bluelink_refresh_token")
if [ -n "$BLUELINK" ]; then
    echo "   -> Bluelink Client aktiv."
    nohup $PYTHON_EXEC bluelink_client.py > /var/www/html/logs/bluelink_client.log 2>&1 &
fi

# EPEX Manager (Neu in V4)
echo "   -> EPEX Manager aktiv."
nohup $PYTHON_EXEC epex_manager.py > /proc/1/fd/1 2>&1 &

# Weather & PV Forecast Manager (Daemon: 60-Min-Zyklus)
# WICHTIG: Erster Fetch laeuft synchron VOR dem Daemon-Start (s.u.),
# damit pv_forecast.json sofort verfuegbar ist.
echo "   -> PV Forecast & Weather Manager aktiv."
nohup $PYTHON_EXEC Forecast/pv_forecast_service.py > /proc/1/fd/1 2>&1 &

# Storage Simulator (Neu in V4)
echo "   -> Storage Simulator aktiv."
nohup $PYTHON_EXEC storage_simulator.py > /proc/1/fd/1 2>&1 &

# Storage Manager (Gehirn Live - Neu in V4.0.5)
echo "   -> Storage Manager (Gehirn Live) aktiv."
nohup $PYTHON_EXEC storage_manager.py > /proc/1/fd/1 2>&1 &

# E3DC Live RSCP Client (Immer aktiv - Python-nativer Datenstream fuer V4 KI/Forecast)
if [ "${E3DC_LIVE_STARTED:-0}" != "1" ]; then
    echo "   -> E3DC Live RSCP Client aktiv."
    nohup $PYTHON_EXEC e3dc_live.py --write --loops 0 --interval 3 > /proc/1/fd/1 2>&1 &
else
    echo "   -> E3DC Live RSCP Client bereits aktiv."
fi

# Notifier / Scheduler (Immer aktiv - ersetzt Cronjobs in Docker)
echo "   -> Notification & Schedule Manager aktiv."
nohup $PYTHON_EXEC notification_manager.py > /var/www/html/logs/notification_manager.log 2>&1 &

# Matter Bridge (optional, read-only Statusendpunkte)
MATTER=$(get_v4_val "matter_bridge")
if [ "$MATTER" = "1" ] || [ "$MATTER" = "true" ]; then
    if [ -d "$MATTER_DIR/node_modules" ]; then
        echo "   -> Matter Bridge aktiv."
        nohup runuser -u www-data -- sh -c "cd '$MATTER_DIR' && exec npm run start" \
            > /var/www/html/logs/matter_bridge.log 2>&1 &
    else
        echo "   -> Matter Bridge kann ohne installierte NPM-Abhängigkeiten nicht starten."
    fi
fi

echo "-> Alle Dienste gestartet. Container laeuft."

# Hauptprozess: warte auf alle Hintergrund-Prozesse.
# (Das C++ E3DC-Control Binary wird nicht mehr benoetigt - alle Funktionen
# sind in den Python-Diensten implementiert.)
wait
