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
    """Fragt den Refresh-Token ab und speichert ihn."""
    config_file = os.path.join(get_install_path(), "e3dc.config.txt")
    curr_token = ""
    curr_vin = ""

    try:
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if '=' in line and not line.strip().startswith('#'):
                        k, v = [x.strip() for x in line.split('=', 1)]
                        if k.lower() == 'bluelink_refresh_token': curr_token = v
                        elif k.lower() == 'bluelink_vin': curr_vin = v
    except: pass

    print("\n=== Bluelink SoC-Abfrage einrichten ===")
    print("Dieses Modul fragt den Ladestand deines Hyundai/Kia direkt ab.")
    print("Du benötigst einen gültigen 'Refresh Token'.\n")

    prompt_t = f"Bitte gib deinen Bluelink Refresh-Token ein [{curr_token[:10]}...]: " if curr_token else "Bitte gib deinen Bluelink Refresh-Token ein: "
    token = input(prompt_t).strip() or curr_token
    if not token:
        print("Abbruch: Kein Token angegeben.")
        return False

    prompt_v = f"Optional: Gib die VIN deines Fahrzeugs ein [{curr_vin}]: " if curr_vin else "Optional: Gib die VIN deines Fahrzeugs ein (leer lassen für erstes Fahrzeug): "
    vin = input(prompt_v).strip() or curr_vin

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            content = f.read()

        def upsert_param(text, key, val):
            pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*=.*$', re.IGNORECASE | re.MULTILINE)
            if pattern.search(text):
                return pattern.sub(f"{key} = {val}", text)
            else:
                return text + f"\n{key} = {val}"

        content = upsert_param(content, "bluelink_refresh_token", token)
        if vin:
            content = upsert_param(content, "bluelink_vin", vin)

        with open(config_file, 'w', encoding='utf-8') as f:
            f.write(content)

        print("✓ Konfiguration erfolgreich gespeichert.")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Speichern der Config: {e}")
        return False

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
