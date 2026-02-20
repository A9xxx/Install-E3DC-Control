#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_JSON="$INSTALL_ROOT/Installer/installer_config.json"
WEB_JSON="/var/www/html/e3dc_paths.json"

read_config_value() {
  local key="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r ".${key} // empty" "$CONFIG_JSON"
  else
    python3 - <<PY
import json
try:
    with open("$CONFIG_JSON", "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("$key", ""))
except Exception:
    print("")
PY
  fi
}

echo "== E3DC Installer Permissions Check =="
echo

if [[ ! -f "$CONFIG_JSON" ]]; then
  echo "[FEHLER] Config-Datei fehlt: $CONFIG_JSON"
  exit 1
fi

INSTALL_USER="$(read_config_value install_user)"
HOME_DIR="$(read_config_value home_dir)"

if [[ -z "$INSTALL_USER" ]]; then
  echo "[FEHLER] install_user konnte aus $CONFIG_JSON nicht gelesen werden"
  exit 1
fi

echo "Install-Root : $INSTALL_ROOT"
echo "Install-User : $INSTALL_USER"
echo "Home-Dir     : ${HOME_DIR:-<leer>}"
echo

echo "-- UID/GID --"
id "$INSTALL_USER" || true
echo

echo "-- Besitzer/Modus prüfen --"
sudo stat -c '%U:%G %a %n' "$CONFIG_JSON" || true
sudo stat -c '%U:%G %a %n' "$WEB_JSON" 2>/dev/null || echo "[Hinweis] $WEB_JSON nicht vorhanden"
echo

echo "-- Zugriffstests --"
if sudo -u "$INSTALL_USER" test -r "$CONFIG_JSON"; then
  echo "[OK] $INSTALL_USER kann installer_config.json lesen"
else
  echo "[FEHLER] $INSTALL_USER kann installer_config.json NICHT lesen"
fi

if sudo -u "$INSTALL_USER" test -w "$CONFIG_JSON"; then
  echo "[OK] $INSTALL_USER kann installer_config.json schreiben"
else
  echo "[FEHLER] $INSTALL_USER kann installer_config.json NICHT schreiben"
fi

if sudo -u www-data test -r "$CONFIG_JSON"; then
  echo "[OK] www-data kann installer_config.json lesen"
else
  echo "[WARNUNG] www-data kann installer_config.json nicht lesen"
fi

echo
if [[ -f "$WEB_JSON" ]]; then
  if sudo -u www-data test -r "$WEB_JSON"; then
    echo "[OK] www-data kann e3dc_paths.json lesen"
  else
    echo "[WARNUNG] www-data kann e3dc_paths.json nicht lesen"
  fi
fi

echo

echo "-- Hardcoding-Scan (pi/home/pi) --"
grep -RInE '\bpi\b|/home/pi|pi:www-data|pi:pi|~pi' "$INSTALL_ROOT" --include='*.py' || true

echo

echo "Fertig."
