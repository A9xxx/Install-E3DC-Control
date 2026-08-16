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

require_exact_ramdisk_tmpfs() {
    local mounted_target=""
    if [ ! -x /usr/bin/findmnt ]; then
        echo "-> FEHLER: /usr/bin/findmnt fehlt; E3DC-Dienste bleiben gestoppt."
        return 1
    fi
    mounted_target="$(
        /usr/bin/findmnt \
            --kernel \
            --first-only \
            --mountpoint "$RAMDISK_DIR" \
            --types tmpfs \
            --noheadings \
            --output TARGET \
            2>/dev/null
    )" || mounted_target=""
    if [ "$mounted_target" != "$RAMDISK_DIR" ]; then
        echo "-> FEHLER: $RAMDISK_DIR ist nicht exakt als tmpfs gemountet."
        echo "   E3DC-Dienste und Apache werden zum Schutz vor persistenten Ersatzschreibzugriffen nicht gestartet."
        return 1
    fi
}

# Docker besitzt kein systemd-Drop-in. Deshalb gilt dieselbe Sperre hier vor
# Apache, PHP und jedem Python-/Node-Dienst.
require_exact_ramdisk_tmpfs

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
    apache2ctl -t
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
    local exit_status="${1:-0}"
    local pids=""
    trap - TERM INT
    echo "-> Container wird beendet: sichere Ramdisk-Warmstartdaten..."
    save_ramdisk_cache || true
    pids="$(jobs -pr)"
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        wait $pids 2>/dev/null || true
    fi
    exit "$exit_status"
}

handle_stop_signal() {
    shutdown_container 0
}

trap handle_stop_signal TERM INT

# Web-Interface aus dem gemounteten Repo in den Apache-Webroot synken.
# Der Code liegt via Volume unter /app/pi/Install/html/, Apache liest aus /var/www/html/.
echo "-> Synke Web-Interface (html/ -> /var/www/html/)..."
WWW_DATA_GID="$(id -g www-data)"
if [ -L /var/www/html ] || [ ! -d /var/www/html ]; then
    echo "-> FEHLER: /var/www/html ist kein eindeutiges Verzeichnis."
    exit 1
fi
# Der Elternname des RAM-Disk-Mountpoints muss schon vor jeder Root-Kopie
# unveränderlich sein. Nur die getrennten Laufzeitunterordner werden später
# für www-data schreibbar gemacht.
chown root:www-data /var/www/html
chmod 0755 /var/www/html
UNSAFE_WEB_ENTRY="$(
    find -P /var/www/html -xdev \
        \( -type l -o \( ! -type d -a ! -type f \) \) \
        -print -quit
)"
if [ -n "$UNSAFE_WEB_ENTRY" ]; then
    echo "-> FEHLER: Unsicherer bestehender Webroot-Eintrag: $UNSAFE_WEB_ENTRY"
    exit 1
fi
if [ -d "/app/pi/Install/html" ]; then
    UNSAFE_WEB_SOURCE="$(
        find -P /app/pi/Install/html -xdev \
            \( -type l -o \( ! -type d -a ! -type f \) \) \
            -print -quit
    )"
    if [ -n "$UNSAFE_WEB_SOURCE" ]; then
        echo "-> FEHLER: Unsicherer Web-Quellbaum: $UNSAFE_WEB_SOURCE"
        exit 1
    fi
    if [ ! -d "/app/pi/Install/html/app" ]; then
        rm -rf /var/www/html/app
    fi
    cp -r /app/pi/Install/html/. /var/www/html/
    for web_kind in d f; do
        if [ "$web_kind" = "d" ]; then
            web_mode=0755
        else
            web_mode=0644
        fi
        find -P /var/www/html -xdev \
            \( -path /var/www/html/data -o -path /var/www/html/logs \
               -o -path /var/www/html/ramdisk -o -path /var/www/html/tmp \) \
            -prune -o -type "$web_kind" \
            -exec chown root:www-data -- {} + \
            -exec chmod "$web_mode" -- {} +
    done
    echo "   -> Web-Interface aktualisiert."
else
    echo "   -> WARNUNG: /app/pi/Install/html nicht gefunden! Repo korrekt gemountet?"
fi

MATTER_DIR="/app/pi/Install/Installer/matter"
MATTER_STORAGE="/var/www/html/data/matter-storage"
MATTER_STORAGE_GUARD="/usr/local/bin/e3dc-docker-matter-storage-guard"

# Rechte des persistenten Daten-Ordners korrigieren
find -P /var/www/html/data -xdev \
    -path "$MATTER_STORAGE" -prune -o \
    \( -type d -o -type f \) \
    -exec chown -h www-data:www-data -- {} +
chmod 2775 /var/www/html/data
if [ ! -f "$MATTER_STORAGE_GUARD" ]; then
    echo "-> FEHLER: Descriptorprüfer für Matter-Storage fehlt."
    exit 1
fi
MATTER_STORAGE_IDENTITY="$(
    /usr/bin/python3 -I -B "$MATTER_STORAGE_GUARD" \
        --mode harden \
        --path "$MATTER_STORAGE" \
        --owner www-data \
        --group www-data
)"
case "$MATTER_STORAGE_IDENTITY" in
    e3dc-matter-storage-v1:*) ;;
    *)
        echo "-> FEHLER: Matter-Storage lieferte keine gebundene Rootidentität."
        exit 1
        ;;
esac
mkdir -p /var/www/html/tmp
chown -R www-data:www-data /var/www/html/tmp
chmod 2775 /var/www/html/tmp

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

# Root-kontrollierter Einzelschreiber-Namespace (nicht in der www-data-Ramdisk).
LOCK_NAMESPACE_ROOT="/run/e3dc-control"
LOCK_DIRECTORY="$LOCK_NAMESPACE_ROOT/locks"
STORAGE_MANAGER_LOCK="$LOCK_DIRECTORY/storage_manager.owner.lock"
WALLBOX_MANAGER_LOCK="$LOCK_DIRECTORY/wallbox_manager.owner.lock"
ENERGY_MANAGER_LOCK="$LOCK_DIRECTORY/energy_manager.owner.lock"
HEIZSTAB_MANAGER_LOCK="$LOCK_DIRECTORY/heizstab_manager.owner.lock"
HEAT_ACTUATOR_ENDPOINTS_LOCK="$LOCK_DIRECTORY/heat_actuator_endpoints.lock"

for lock_dir in "$LOCK_NAMESPACE_ROOT" "$LOCK_DIRECTORY"; do
    if [ -L "$lock_dir" ] || { [ -e "$lock_dir" ] && [ ! -d "$lock_dir" ]; }; then
        echo "-> FEHLER: Unsicherer Manager-Locknamespace: $lock_dir"
        exit 1
    fi
    install -d -o root -g root -m 0755 -- "$lock_dir"
    if [ "$(stat -c '%u:%g:%a' -- "$lock_dir")" != "0:0:755" ]; then
        echo "-> FEHLER: Manager-Lockverzeichnis ist nicht root:root 0755: $lock_dir"
        exit 1
    fi
done

for lock_file in \
    "$STORAGE_MANAGER_LOCK" \
    "$WALLBOX_MANAGER_LOCK" \
    "$ENERGY_MANAGER_LOCK" \
    "$HEIZSTAB_MANAGER_LOCK" \
    "$HEAT_ACTUATOR_ENDPOINTS_LOCK"; do
    if [ -L "$lock_file" ] || { [ -e "$lock_file" ] && [ ! -f "$lock_file" ]; }; then
        echo "-> FEHLER: Unsichere Manager-Lockdatei: $lock_file"
        exit 1
    fi
    if [ ! -e "$lock_file" ]; then
        install -o root -g www-data -m 0660 /dev/null "$lock_file"
    fi
    if [ "$(stat -c '%h' -- "$lock_file")" != "1" ]; then
        echo "-> FEHLER: Manager-Lockdatei besitzt nicht genau einen Hardlink: $lock_file"
        exit 1
    fi
    chown root:www-data -- "$lock_file"
    chmod 0660 -- "$lock_file"
    if [ "$(stat -c '%u:%g:%a' -- "$lock_file")" != "0:${WWW_DATA_GID}:660" ]; then
        echo "-> FEHLER: Manager-Lockdatei ist nicht root:www-data 0660: $lock_file"
        exit 1
    fi
done

# Logs-Ordner: www-data + Python-Prozesse muessen schreiben duerfen
mkdir -p /var/www/html/logs
chown -R www-data:www-data /var/www/html/logs
chmod -R 775 /var/www/html/logs
if [ "$(stat -c '%u:%g:%a' -- /var/www/html)" != "0:${WWW_DATA_GID}:755" ]; then
    echo "-> FEHLER: Webroot schützt den RAM-Disk-Namensraum nicht dauerhaft."
    exit 1
fi

# Nutze das sichere VENV aus dem Container
PYTHON_EXEC="/opt/venv/bin/python3"

# Führe V4-Migration aus, falls alte e3dc.config.txt existiert und Keys in e3dc_v4.json fehlen
echo "-> Prüfe und migriere alte Konfiguration..."
cd /app/pi/Install
if ! "$PYTHON_EXEC" - "$V4_CONFIG" <<'PY'
import json
import sys

from Installer.config_manager import _save_v4, run_config_wizard
from Installer.installer_config import apply_web_config_start_defaults
from Installer.ha_writer_admission import project_instance_role_anchor


try:
    with open(sys.argv[1], "r", encoding="utf-8") as config_file:
        config = json.load(config_file)
except Exception as exc:
    print(f"V4-Konfiguration ist vor der Migration nicht lesbar: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(config, dict):
    print("e3dc_v4.json muss ein JSON-Objekt enthalten.", file=sys.stderr)
    raise SystemExit(1)
if config == {}:
    first_install_config = apply_web_config_start_defaults(
        config,
        first_install=True,
    )
    if first_install_config.get("ha_mode") != "off" or not _save_v4(
        first_install_config
    ):
        print("Docker-Erstkonfiguration konnte nicht atomar gebunden werden.", file=sys.stderr)
        raise SystemExit(1)

if run_config_wizard() is not True:
    print("run_config_wizard hat keinen erfolgreichen Abschluss gemeldet.", file=sys.stderr)
    raise SystemExit(1)

try:
    with open(sys.argv[1], "r", encoding="utf-8") as config_file:
        config = json.load(config_file)
except Exception as exc:
    print(f"V4-Konfiguration ist nach der Migration nicht lesbar: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(config, dict):
    print("e3dc_v4.json muss nach der Migration ein JSON-Objekt enthalten.", file=sys.stderr)
    raise SystemExit(1)
if config.get("ha_mode") != "off":
    print(
        "Docker ist ausschließlich mit ha_mode=off zulässig; "
        "HA- und Shadow-Betrieb bleiben Bare-Metal-Funktionen.",
        file=sys.stderr,
    )
    raise SystemExit(1)
if project_instance_role_anchor("off") is not True:
    print(
        "Der persistente Docker-Instanzrollenanker ist nicht create-once auf ha_mode=off gebunden.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
then
    echo "-> FEHLER: Konfigurationsmigration oder Docker-Standalone-Guard fehlgeschlagen."
    exit 1
fi

if [ ! -x /usr/local/bin/e3dc-docker-logrotate ] \
    || [ ! -f /etc/logrotate.d/e3dc-control ] \
    || [ -L /etc/logrotate.d/e3dc-control ] \
    || [ "$(stat -c '%u:%g:%a:%h' -- /etc/logrotate.d/e3dc-control)" != "0:0:644:1" ]; then
    echo "-> FEHLER: Docker-Logrotate besitzt keinen sicheren Image-Vertrag."
    exit 1
fi
LOGROTATE_HEALTH="$LOCK_NAMESPACE_ROOT/docker_logrotate_health.json"
if [ -L "$LOGROTATE_HEALTH" ] || { [ -e "$LOGROTATE_HEALTH" ] && [ ! -f "$LOGROTATE_HEALTH" ]; }; then
    echo "-> FEHLER: Unsicherer alter Docker-Logrotate-Healthpfad."
    exit 1
fi
if [ -e "$LOGROTATE_HEALTH" ] \
    && [ "$(stat -c '%u:%g:%a:%h' -- "$LOGROTATE_HEALTH")" != "0:0:444:1" ]; then
    echo "-> FEHLER: Alter Docker-Logrotate-Healthnachweis verletzt den Root-Vertrag."
    exit 1
fi
rm -f -- "$LOGROTATE_HEALTH"
echo "-> Starte fail-closed Logrotation für persistente Produktlogs..."
nohup "$PYTHON_EXEC" -I -B /usr/local/bin/e3dc-docker-logrotate > /proc/1/fd/1 2>&1 &
LOGROTATE_PID=$!
LOGROTATE_READY=0
for _logrotate_wait in $(seq 1 15); do
    if ! kill -0 "$LOGROTATE_PID" 2>/dev/null; then
        echo "-> FEHLER: Docker-Logrotate endete vor dem ersten Erfolgsnachweis."
        wait "$LOGROTATE_PID" || true
        exit 1
    fi
    if [ -f "$LOGROTATE_HEALTH" ] \
        && [ ! -L "$LOGROTATE_HEALTH" ] \
        && [ "$(stat -c '%u:%g:%a:%h' -- "$LOGROTATE_HEALTH")" = "0:0:444:1" ]; then
        LOGROTATE_READY=1
        break
    fi
    sleep 1
done
if [ "$LOGROTATE_READY" -ne 1 ]; then
    echo "-> FEHLER: Docker-Logrotate lieferte keinen sicheren ersten Erfolgsnachweis."
    exit 1
fi

echo "-> Starte Apache Webserver als überwachten Vordergrundprozess..."
configure_apache_web_port
apache2ctl -D FOREGROUND > /proc/1/fd/1 2>&1 &
APACHE_PID=$!

echo "-> Starte Python Hintergrunddienste..."

# Die V4-Konfiguration wird exakt einmal gelesen. Derselbe kanonische Vertrag
# entscheidet damit für systemd und Docker, welche Zusatzdienste gewollt sind.
# Import-, JSON- und Vertragsfehler stoppen den Container vor dem ersten
# optionalen Prozess; eine Shell-Nachbildung der Aktivierungslogik gibt es nicht.
if ! OPTIONAL_SERVICE_PROJECTION="$(
    "$PYTHON_EXEC" - "$V4_CONFIG" <<'PY'
import json
import sys

SUPPORTED_DOCKER_OPTIONALS = (
    "e3dc-wallbox-manager",
    "energy_manager",
    "e3dc-lux-live",
    "e3dc-idm-live",
    "e3dc-stiebel-live",
    "e3dc-dimplex-live",
    "e3dc-heizstab",
    "e3dc-climate-live",
    "e3dc-climate-control",
    "e3dc-forecast-evidence",
    "e3dc-matter-bridge",
    "e3dc-bluelink",
    "e3dc-mqtt-hub",
)

try:
    from Installer.optional_service_contract import configured_optional_services

    config_path = sys.argv[1]
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise ValueError("e3dc_v4.json muss ein JSON-Objekt enthalten")

    configured = configured_optional_services(config)
    unsupported = tuple(
        service for service in configured if service not in SUPPORTED_DOCKER_OPTIONALS
    )
    if unsupported:
        raise ValueError(
            "Docker-Startabbildung fehlt für: " + ", ".join(unsupported)
        )
    if len(configured) != len(set(configured)):
        raise ValueError("Optionale Dienste dürfen nicht doppelt projiziert werden")

    for service in configured:
        print(service)
except Exception as exc:
    print(f"Optionale Dienstprojektion fehlgeschlagen: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
)"; then
    echo "-> FEHLER: Optionale Docker-Dienste konnten nicht fail-closed projiziert werden."
    exit 1
fi

optional_service_selected() {
    local requested_service="$1"
    local configured_service=""
    while IFS= read -r configured_service; do
        if [ "$configured_service" = "$requested_service" ]; then
            return 0
        fi
    done <<EOF
$OPTIONAL_SERVICE_PROJECTION
EOF
    return 1
}

# Der Healthcheck prüft exakt den bei diesem Boot kanonisch projizierten
# Zusatzdienstsatz. Spätere Konfigurationsänderungen werden erst mit dem
# dokumentierten Container-Neustart wirksam und erzeugen keinen Parallelvertrag.
DOCKER_HEALTH_OPTIONALS="/run/e3dc-control/docker_health_optional_services"
DOCKER_HEALTH_OPTIONALS_TMP="${DOCKER_HEALTH_OPTIONALS}.tmp.$$"
if [ -L "$DOCKER_HEALTH_OPTIONALS" ] \
    || { [ -e "$DOCKER_HEALTH_OPTIONALS" ] && [ ! -f "$DOCKER_HEALTH_OPTIONALS" ]; }; then
    echo "-> FEHLER: Unsichere Docker-Health-Projektion: $DOCKER_HEALTH_OPTIONALS"
    exit 1
fi
rm -f -- "$DOCKER_HEALTH_OPTIONALS_TMP"
(
    umask 077
    printf '%s\n' "$OPTIONAL_SERVICE_PROJECTION" > "$DOCKER_HEALTH_OPTIONALS_TMP"
)
chown root:root -- "$DOCKER_HEALTH_OPTIONALS_TMP"
chmod 0444 -- "$DOCKER_HEALTH_OPTIONALS_TMP"
mv -f -- "$DOCKER_HEALTH_OPTIONALS_TMP" "$DOCKER_HEALTH_OPTIONALS"
if [ "$(stat -c '%u:%g:%a:%h' -- "$DOCKER_HEALTH_OPTIONALS")" != "0:0:444:1" ]; then
    echo "-> FEHLER: Docker-Health-Projektion besitzt keinen sicheren Endvertrag."
    exit 1
fi

if optional_service_selected "e3dc-matter-bridge"; then
    if [ ! -f "$MATTER_DIR/package.json" ] \
        || [ ! -f "$MATTER_DIR/package-lock.json" ]; then
        echo "-> FEHLER: Matter Bridge ist aktiviert, aber Paket- oder Lockdatei fehlt."
        exit 1
    fi
    if [ ! -d "$MATTER_DIR/node_modules" ]; then
        echo "-> Installiere Matter-Abhängigkeiten..."
        cd "$MATTER_DIR"
        npm ci --omit=dev --ignore-scripts
    fi
fi

cd /app/pi/Install/Installer

# E3DC Live RSCP Client ZUERST starten -- liefert live_data_py.json fuer storage_simulator.
# Ohne diesen Schritt wuerde storage_simulator mit SOC=0% starten -> Discharge-Sperre!
echo "-> E3DC Live RSCP Client (zuerst, liefert Live-SoC)..."
nohup $PYTHON_EXEC e3dc_live.py --write --loops 0 --interval 3 > /proc/1/fd/1 2>&1 &
E3DC_LIVE_STARTED=1
echo "   -> Warte 10s auf erste RSCP-Messung..."
sleep 10

# === INITIALER SOFORT-FORECAST (VOR Simulator-Start!) ===
echo "-> Initialer PV-Forecast (--once, vor Daemon-Start)..."
if $PYTHON_EXEC Forecast/pv_forecast_service.py --once >> /proc/1/fd/1 2>&1; then
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

# Klimaanlage Live (read-only, nur bei kanonischer Aktivierung)
if optional_service_selected "e3dc-climate-live"; then
    echo "   -> Klimaanlagen-Monitor (read-only) aktiv."
    nohup $PYTHON_EXEC climate_live.py > /var/www/html/logs/climate_live.log 2>&1 &
fi

# Klimaanlagen-Regelstatus (read-only, nur bei expliziter Aktivierung)
if optional_service_selected "e3dc-climate-control"; then
    echo "   -> Klimaanlagen-Regelstatus (read-only) aktiv."
    nohup $PYTHON_EXEC climate_control.py > /var/www/html/logs/climate_control.log 2>&1 &
fi

# Energy Manager (nur bei kanonisch vollständig konfigurierter Wärmekopplung)
if optional_service_selected "energy_manager"; then
    echo "   -> Energy Manager aktiv."
    nohup $PYTHON_EXEC luxtronik/energy_manager.py > /proc/1/fd/1 2>&1 &
fi

# Luxtronik Live-Daten (NUR wenn Wärmepumpe explizit aktiviert)
if optional_service_selected "e3dc-lux-live"; then
    echo "   -> Luxtronik WebSocket-Client aktiv."
    nohup $PYTHON_EXEC luxtronik/lux_live.py > /var/www/html/logs/lux_live.log 2>&1 &
fi

# IDM Live-Daten (NUR wenn IDM-IP konfiguriert)
if optional_service_selected "e3dc-idm-live"; then
    echo "   -> IDM Modbus-Client aktiv."
    nohup $PYTHON_EXEC idm/idm_live.py > /var/www/html/logs/idm_live.log 2>&1 &
fi

# Stiebel ISG Live-Daten (read-only)
if optional_service_selected "e3dc-stiebel-live"; then
    echo "   -> Stiebel ISG Live aktiv."
    nohup $PYTHON_EXEC stiebel/stiebel_live.py > /var/www/html/logs/stiebel_live.log 2>&1 &
fi

# Dimplex WPM Live-Daten (read-only, nur bei vollständiger expliziter Freigabe)
if optional_service_selected "e3dc-dimplex-live"; then
    echo "   -> Dimplex WPM Live aktiv."
    nohup $PYTHON_EXEC dimplex/dimplex_live.py > /var/www/html/logs/dimplex_live.log 2>&1 &
fi

# Heizstab Manager (nur bei kanonisch vollständiger Konfiguration)
if optional_service_selected "e3dc-heizstab"; then
    echo "   -> Heizstab Manager aktiv."
    nohup $PYTHON_EXEC heizstab_manager.py > /var/www/html/logs/heizstab_manager.log 2>&1 &
fi

# Native Wallbox Manager (Python PID-Regler für Go-e etc.)
if optional_service_selected "e3dc-wallbox-manager"; then
    echo "   -> Native Wallbox Manager aktiv."
    nohup $PYTHON_EXEC wallbox_manager.py > /proc/1/fd/1 2>&1 &
fi

# MQTT Hub (wenn in config konfiguriert)
if optional_service_selected "e3dc-mqtt-hub"; then
    echo "   -> MQTT Hub aktiv."
    nohup $PYTHON_EXEC e3dc_mqtt_hub.py > /proc/1/fd/1 2>&1 &
fi

# Bluelink (wenn konfiguriert)
if optional_service_selected "e3dc-bluelink"; then
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

# Rein diagnostische PV-Prognosediagnose. Ohne ausdrückliche Aktivierung wird
# weder ein Prozess gestartet noch E3/DC-Historie gelesen oder eine DB erzeugt.
if optional_service_selected "e3dc-forecast-evidence"; then
    FORECAST_EVIDENCE_DIR="/var/lib/e3dc-control/forecast-evidence"
    install -d -o root -g root -m 0700 "$FORECAST_EVIDENCE_DIR"
    echo "   -> PV-Prognosediagnose (read-only, niedrige Priorität) aktiv."
    nohup nice -n 10 ionice -c 3 "$PYTHON_EXEC" forecast_evidence_sidecar.py \
        > /proc/1/fd/1 2>&1 &
else
    echo "   -> PV-Prognosediagnose ausgeschaltet (keine Historienabfrage/DB)."
fi

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
if optional_service_selected "e3dc-matter-bridge"; then
    echo "-> Starte D-Bus und Avahi für Matter mDNS..."
    if ! service dbus start >/dev/null 2>&1 \
        || [ ! -S /run/dbus/system_bus_socket ]; then
        echo "-> FEHLER: D-Bus konnte für Matter nicht sicher gestartet werden."
        exit 1
    fi
    if [ ! -x /usr/sbin/avahi-daemon ]; then
        echo "-> FEHLER: Avahi-Daemon fehlt; Matter mDNS bleibt gestoppt."
        exit 1
    fi
    /usr/sbin/avahi-daemon -s &
    AVAHI_PID=$!
    AVAHI_READY=0
    for _avahi_wait in $(seq 1 25); do
        if ! kill -0 "$AVAHI_PID" 2>/dev/null; then
            echo "-> FEHLER: Avahi endete vor dem mDNS-Bereitschaftsnachweis."
            wait "$AVAHI_PID" || true
            exit 1
        fi
        if /usr/sbin/avahi-daemon --check >/dev/null 2>&1; then
            AVAHI_READY=1
            break
        fi
        sleep 0.2
    done
    if [ "$AVAHI_READY" -ne 1 ]; then
        echo "-> FEHLER: Avahi lieferte keinen mDNS-Bereitschaftsnachweis."
        kill "$AVAHI_PID" 2>/dev/null || true
        wait "$AVAHI_PID" || true
        exit 1
    fi
    echo "   -> Matter Bridge aktiv."
    if ! /usr/bin/python3 -I -B "$MATTER_STORAGE_GUARD" \
        --mode verify \
        --path "$MATTER_STORAGE" \
        --owner www-data \
        --group www-data \
        --expected-identity "$MATTER_STORAGE_IDENTITY" \
        >/dev/null; then
        echo "-> FEHLER: Matter-Storage driftete vor dem Workerstart."
        exit 1
    fi
    nohup runuser -u www-data -- sh -c "umask 077; cd '$MATTER_DIR' && exec npm run start" \
        > /var/www/html/logs/matter_bridge.log 2>&1 &
fi

echo "-> Alle Dienste gestartet. Container laeuft."

# PID 1 beendet den Container beim ersten unerwartet beendeten Kindprozess.
# Das umfasst den Apache-Vordergrundprozess und alle Python-/Node-Worker. Auch
# ein sauberer Kind-Exit ist hier ein Fehler, weil diese Dienste dauerhaft
# laufen müssen; Docker kann den vollständigen Dienstsatz danach neu starten.
if wait -n; then
    child_status=1
else
    child_status=$?
    if [ "$child_status" -eq 0 ]; then
        child_status=1
    fi
fi
echo "-> FEHLER: Ein überwachter Containerdienst wurde beendet (Status $child_status)."
shutdown_container "$child_status"
