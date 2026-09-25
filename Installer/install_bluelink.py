import os
import re
import subprocess
import tempfile
from .core import register_command
from .utils import run_command, pip_install
from .installer_config import get_install_path, get_install_user, load_config
from .logging_manager import get_or_create_logger, log_task_completed

logger = get_or_create_logger("bluelink_client")


def write_bluelink_service_unit(service_content):
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name
        cp_result = subprocess.run(
            ["sudo", "cp", tmp_path, "/etc/systemd/system/e3dc-bluelink.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        if cp_result.returncode != 0:
            logger.error("Bluelink-Service konnte nicht geschrieben werden: %s", cp_result.stderr.strip())
            print(f"  ✗ Fehler beim Schreiben der Service-Datei: {cp_result.stderr.strip()}")
            return False
        chmod_result = subprocess.run(
            ["sudo", "chmod", "644", "/etc/systemd/system/e3dc-bluelink.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        if chmod_result.returncode != 0:
            logger.error("Bluelink-Service-Rechte konnten nicht gesetzt werden: %s", chmod_result.stderr.strip())
            print(f"  ✗ Fehler beim Setzen der Service-Rechte: {chmod_result.stderr.strip()}")
            return False
        return True
    except OSError as exc:
        logger.error("Bluelink-Service konnte nicht vorbereitet werden: %s", exc)
        print(f"  ✗ Fehler beim Vorbereiten der Service-Datei: {exc}")
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

def configure_bluelink():
    """Zugangsdaten werden nicht mehr im Terminal abgefragt.

    Benutzer (E-Mail), Passwort und optionale PIN des Hyundai/Kia-Kontos gehören in die zugriffsgeschützte
    V4-Konfiguration (Konfigurations-Editor, Gruppe Fahrzeug Integration (Bluelink)); ein alter Refresh-Token in
    e3dc.config.txt wird nicht mehr verwendet.
    """
    config_file = os.path.join(get_install_path(), "e3dc.config.txt")
    legacy_token = False
    try:
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if '=' in line and not line.strip().startswith('#'):
                        k, v = [x.strip() for x in line.split('=', 1)]
                        if k.lower() == 'bluelink_refresh_token' and v:
                            legacy_token = True
    except Exception:
        pass

    print("\n=== Bluelink SoC-Abfrage einrichten ===")
    print("Dieses Modul fragt den Ladestand deines Hyundai/Kia direkt beim Hersteller ab.")
    print("Die Anmeldung erfolgt mit Benutzer (E-Mail) und Passwort des Hyundai-/Kia-Kontos.")
    print("Trage die Zugangsdaten im Web-Dashboard unter Konfiguration > Fahrzeug Integration (Bluelink) ein;")
    print("hier im Installer werden keine Passwörter abgefragt oder gespeichert.")
    if legacy_token:
        print("Hinweis: Der in e3dc.config.txt gefundene Refresh-Token wird nicht mehr verwendet.")
    return True

def setup_bluelink_service():
    """Installiert Abhängigkeiten und richtet den Timer-Dienst ein."""

    install_user = get_install_user()
    installer_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(installer_dir, "bluelink_client.py")

    venv_name = load_config().get("venv_name", ".venv_e3dc")
    python_bin = "/usr/bin/python3"
    from .installer_config import get_home_dir, get_install_path

    venv_path = ""
    if venv_name:
        if os.path.exists(os.path.join(get_home_dir(install_user), venv_name)):
            venv_path = os.path.join(get_home_dir(install_user), venv_name)
        elif os.path.exists(os.path.join(get_install_path(), venv_name)):
            venv_path = os.path.join(get_install_path(), venv_name)

    print("\n→ Installiere Python-Abhängigkeit (hyundai_kia_connect_api)...")
    if venv_path and os.path.exists(os.path.join(venv_path, "bin", "python3")):
        python_bin = os.path.join(venv_path, "bin", "python3")
        venv_pip = os.path.join(venv_path, "bin", "pip")
        res = subprocess.run(
            ["sudo", "-u", install_user, venv_pip, "install", "hyundai_kia_connect_api"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            print("  ✓ Paket erfolgreich im venv installiert.")
        else:
            print(f"  ✗ Fehler bei der Installation: {res.stderr}")
    else:
        pip_install("hyundai_kia_connect_api")

    # Service-Datei für den Aufruf
    service_content = f"""[Unit]
Description=E3DC Bluelink SoC Fetcher
After=network-online.target

[Service]
Type=simple
User={install_user}
Group=www-data
ExecStart={python_bin} {script_path}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
    if not write_bluelink_service_unit(service_content):
        return False

    print("→ Bereinige alte Timer falls vorhanden...")
    run_command("sudo systemctl stop e3dc-bluelink.timer")
    run_command("sudo systemctl disable e3dc-bluelink.timer")
    run_command("sudo rm -f /etc/systemd/system/e3dc-bluelink.timer")

    print("→ Aktiviere und starte den Service...")
    run_command("sudo systemctl daemon-reload")
    run_command("sudo systemctl enable --now e3dc-bluelink.service")
    run_command("sudo systemctl restart e3dc-bluelink.service")

    print("\n✓ Bluelink-Client ist eingerichtet und wird alle 15 Minuten ausgeführt.")
    print("  Führe 'sudo journalctl -u e3dc-bluelink.service -n 20' aus, um das Log zu prüfen.")
    return True

def install_bluelink_menu():
    if configure_bluelink() and setup_bluelink_service():
        log_task_completed("Bluelink Client eingerichtet")

register_command("43", "Hyundai/Kia SoC-Abfrage (Bluelink)", install_bluelink_menu, sort_order=43)
