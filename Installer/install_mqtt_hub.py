import json
import os
import re

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
from .utils import pip_install, run_command

logger = get_or_create_logger("mqtt_hub")


def _upsert_legacy_param(text, key, val):
    if re.search(r"^\s*" + re.escape(key) + r"\s*=", text, re.IGNORECASE | re.MULTILINE):
        return re.sub(
            r"^\s*" + re.escape(key) + r"\s*=.*$",
            f"{key} = {val}",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    return text + f"\n{key} = {val}"


def _write_legacy_config_if_present(values):
    config_file = os.path.join(get_install_path(), "e3dc.config.txt")
    if not os.path.exists(config_file):
        return False
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            content = f.read()
        for key, val in values.items():
            content = _upsert_legacy_param(content, key, val)
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as exc:
        logger.warning("Legacy e3dc.config.txt konnte nicht aktualisiert werden: %s", exc)
        return False


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

    tmp_file = WEB_CONFIG_FILE + ".tmp"
    os.makedirs(os.path.dirname(WEB_CONFIG_FILE), exist_ok=True)
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_file, WEB_CONFIG_FILE)
    run_command(f"sudo chown {install_user}:www-data {WEB_CONFIG_FILE}")
    run_command(f"sudo chmod {config_secret_file_mode_text(data)} {WEB_CONFIG_FILE}")
    apply_config_secret_permissions(WEB_CONFIG_FILE, install_user=install_user, data=data)


def configure_mqtt_hub():
    """Interaktive Abfrage und Speicherung der MQTT-Einstellungen."""
    print("\n=== Smart Home MQTT-Hub Setup ===")
    print("Dieses Modul sendet alle Live-Daten (PV, SoC, Wärmepumpe, Wallbox, Preise)")
    print("im Sekundentakt an dein Smart Home System (z.B. Home Assistant).\n")

    ip = input("IP-Adresse des MQTT-Brokers (z.B. IP von Home Assistant): ").strip()
    if not ip:
        print("Abbruch: Es muss eine IP angegeben werden.")
        return False

    port = input("Port [1883]: ").strip() or "1883"
    user = input("Benutzername (leer für anonym): ").strip()
    password = input("Passwort (leer für keines): ").strip()
    topic = input("Basis-Topic [e3dc]: ").strip() or "e3dc"

    values = {
        "mqtt_hub_ip": ip,
        "mqtt_hub_port": port,
        "mqtt_hub_user": user,
        "mqtt_hub_pass": password,
        "mqtt_hub_topic": topic,
    }

    try:
        _write_v4_config(values)
        legacy_written = _write_legacy_config_if_present(values)
        print("[OK] Konfiguration erfolgreich in e3dc_v4.json gespeichert.")
        if legacy_written:
            print("     Legacy e3dc.config.txt wurde zusätzlich aktualisiert.")
        return True
    except Exception as e:
        print(f"[FEHLER] Fehler beim Speichern der Config: {e}")
        return False


def setup_mqtt_service():
    """Installiert Python-Modul und richtet den Service ein."""
    print("\n-> Prüfe Abhängigkeiten (paho-mqtt)...")
    pip_install("paho-mqtt")

    install_user = get_install_user()
    script_path = os.path.join(os.path.dirname(__file__), "e3dc_mqtt_hub.py")
    run_command(f"sudo chmod +x {script_path}")

    cfg = load_config()
    venv_name = cfg.get("venv_name", ".venv_e3dc")
    python_bin = "/usr/bin/python3"
    abs_venv_python = (
        os.path.join(get_home_dir(install_user), venv_name, "bin", "python3")
        if venv_name
        else ""
    )
    if abs_venv_python and os.path.exists(abs_venv_python):
        python_bin = abs_venv_python

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
    with open("temp_mqtt.service", "w", encoding="utf-8") as f:
        f.write(service_content)

    run_command("sudo mv temp_mqtt.service /etc/systemd/system/e3dc-mqtt-hub.service")
    run_command("sudo systemctl daemon-reload && sudo systemctl enable e3dc-mqtt-hub && sudo systemctl restart e3dc-mqtt-hub")

    print("\n[OK] MQTT Hub erfolgreich installiert und gestartet!")
    log_task_completed("MQTT Hub installiert")


def install_mqtt_hub_menu():
    if configure_mqtt_hub():
        setup_mqtt_service()


register_command("48", "Smart Home MQTT-Hub (HA/ioBroker)", install_mqtt_hub_menu, sort_order=48)
