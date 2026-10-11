import json
import os
import re
import getpass
import shlex
import tempfile

from .core import register_command
from .installer_config import (
    WEB_CONFIG_FILE,
    apply_web_config_start_defaults,
    ensure_web_config,
    get_home_dir,
    get_install_path,
    get_install_user,
    load_config,
)
from .config_secret_permissions import apply_config_secret_permissions, config_secret_file_mode_text
from .logging_manager import get_or_create_logger, log_task_completed
from .utils import pip_install, run_command, resolve_venv_target, require_bound_venv_runtime

logger = get_or_create_logger("mqtt_hub")


def _write_v4_config(values):
    install_user = get_install_user()
    ensure_web_config(install_user)
    data = {}
    if os.path.exists(WEB_CONFIG_FILE):
        try:
            with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data = apply_web_config_start_defaults(data)
    data.update(values)

    os.makedirs(os.path.dirname(WEB_CONFIG_FILE), exist_ok=True)
    fd, tmp_file = tempfile.mkstemp(prefix=".mqtt-config-", dir=os.path.dirname(WEB_CONFIG_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for command in (
            f"sudo chown {shlex.quote(install_user)}:www-data {shlex.quote(tmp_file)}",
            f"sudo chmod {config_secret_file_mode_text(data)} {shlex.quote(tmp_file)}",
        ):
            result = run_command(command)
            if not isinstance(result, dict) or not result.get("success"):
                raise RuntimeError("MQTT-Konfiguration konnte nicht mit sicheren Rechten vorbereitet werden")
        if apply_config_secret_permissions(tmp_file, install_user=install_user, data=data) is not True:
            raise RuntimeError("MQTT-Konfigurationsrechte konnten nicht bestätigt werden")
        os.replace(tmp_file, WEB_CONFIG_FILE)
        return True
    finally:
        if os.path.exists(tmp_file):
            os.unlink(tmp_file)



def configure_mqtt_hub():
    """Interaktive Abfrage und Speicherung der MQTT-Einstellungen."""
    print("\n=== Smart Home MQTT-Hub Setup ===")
    print("Dieses Modul sendet alle Live-Daten (PV, SoC, Wärmepumpe, Wallbox, Preise)")
    print("im Sekundentakt an das Smart-Home-System (z.B. Home Assistant).\n")

    ip = input("IP-Adresse des MQTT-Brokers (z.B. IP von Home Assistant): ").strip()
    if not ip:
        print("Abbruch: Es muss eine IP angegeben werden.")
        return False

    port = input("Port [1883]: ").strip() or "1883"
    user = input("Benutzername (leer für anonym): ").strip()
    password = getpass.getpass("Passwort (leer für keines): ")
    topic = input("Basis-Topic [e3dc]: ").strip() or "e3dc"

    values = {
        "mqtt_hub_ip": ip,
        "mqtt_hub_port": port,
        "mqtt_hub_user": user,
        "mqtt_hub_pass": password,
        "mqtt_hub_topic": topic,
    }

    try:
        if _write_v4_config(values) is not True:
            return False
        print("[OK] Konfiguration erfolgreich in e3dc_v4.json gespeichert.")
        return True
    except Exception as e:
        print(f"[FEHLER] Fehler beim Speichern der Config: {e}")
        return False


def setup_mqtt_service():
    """Installiert Python-Modul und richtet den Service ein."""
    print("\n-> Prüfe Abhängigkeiten (paho-mqtt)...")
    if pip_install("paho-mqtt", require_venv=True) is not True:
        print("✗ MQTT-Abhängigkeiten fehlen; Konsole 48 nach der venv-Reparatur erneut starten.")
        return False

    install_user = get_install_user()
    script_path = os.path.join(os.path.dirname(__file__), "e3dc_mqtt_hub.py")
    try:
        _venv_name, venv_path = resolve_venv_target(install_user)
        python_bin = require_bound_venv_runtime(install_user=install_user, venv_path=venv_path)["python"]
    except Exception as exc:
        print(f"✗ Gebundenes venv fehlt: {exc}. Hauptmenü 8) Systempakete vorbereiten ausführen.")
        return False

    print("-> Erstelle Systemd Service 'e3dc-mqtt-hub'...")
    service_content = f"""[Unit]
Description=E3DC Smart Home MQTT Hub
After=network.target

[Service]
Type=simple
User={install_user}
Group=www-data
ExecStart={python_bin} {script_path}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="mqtt-unit-", delete=False) as handle:
            handle.write(service_content)
            tmp_name = handle.name
        for command in (
            f"sudo install -o root -g root -m 644 {shlex.quote(tmp_name)} /etc/systemd/system/e3dc-mqtt-hub.service",
            "sudo systemctl daemon-reload",
            "sudo systemctl enable e3dc-mqtt-hub",
            "sudo systemctl restart e3dc-mqtt-hub",
            "systemctl is-active e3dc-mqtt-hub",
        ):
            result = run_command(command)
            if not isinstance(result, dict) or not result.get("success"):
                print("✗ MQTT-Hub konnte nicht vollständig eingerichtet werden. Nächster Schritt: Hauptmenü 2) Systemstatus anzeigen öffnen.")
                return False
        print("\n[OK] MQTT-Hub erfolgreich installiert und gestartet!")
        log_task_completed("MQTT Hub installiert")
        return True
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def install_mqtt_hub_menu():
    if configure_mqtt_hub() is not True:
        return False
    return setup_mqtt_service()


register_command("48", "Smart Home MQTT-Hub (HA/ioBroker)", install_mqtt_hub_menu, sort_order=48)
