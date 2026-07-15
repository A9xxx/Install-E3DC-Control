import os
import subprocess
import time
import getpass
import sys
import ipaddress

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from .core import register_command
from .utils import run_command, replace_in_file
from .installer_config import get_install_path, get_install_user, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
ha_logger = get_or_create_logger("ha_installer")

def validate_peer_ip(peer_ip):
    try:
        value = str(peer_ip or "").strip()
        if not value:
            return ""
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""

def setup_ssh_keys(user, peer_ip):
    peer_ip = validate_peer_ip(peer_ip)
    if not peer_ip:
        print("✗ Ungültige Peer-IP. Bitte eine numerische IPv4- oder IPv6-Adresse verwenden.")
        return False

    print(f"\n--- SSH Schlüsselaustausch mit {peer_ip} ---")
    print("Damit die Daten automatisch synchronisiert werden können, müssen die")
    print("Raspberry Pis ohne Passwort miteinander kommunizieren können.")
    
    # 1. Prüfen ob lokaler Key existiert
    home_dir = os.path.expanduser(f"~{user}")
    key_path = os.path.join(home_dir, ".ssh", "id_ed25519")
    
    if not os.path.exists(key_path):
        print("Erstelle neuen SSH-Schlüssel...")
        run_command(f"sudo -u {user} ssh-keygen -t ed25519 -N '' -f {key_path}")
    else:
        print("Lokaler SSH-Schlüssel existiert bereits.")

    # 2. Key auf den anderen Pi kopieren
    print(f"\nBitte gib nun das Passwort für den Benutzer '{user}' auf dem ANDEREN Pi ({peer_ip}) ein.")
    print("Hinweis: Bei der ersten Verbindung musst du eventuell 'yes' tippen, um den Fingerprint zu akzeptieren.")
    
    # ssh-copy-id interaktiv ausführen, da wir das Passwort nicht im Script abfragen/speichern wollen
    try:
        subprocess.run(
            ["sudo", "-u", user, "ssh-copy-id", "-i", f"{key_path}.pub", f"{user}@{peer_ip}"],
            check=False,
        )
        
        # Testen
        print("\nTeste Verbindung...")
        test_proc = subprocess.run(
            [
                "sudo", "-u", user,
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=5",
                f"{user}@{peer_ip}",
                "echo", "success",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        test_res = {
            "success": test_proc.returncode == 0,
            "stdout": test_proc.stdout or "",
            "stderr": test_proc.stderr or "",
        }
        
        if test_res['success'] and 'success' in test_res['stdout']:
            print("✓ SSH-Verbindung erfolgreich eingerichtet!")
            return True
        else:
            print("✗ SSH-Verbindungstest fehlgeschlagen. Bitte manuell prüfen.")
            return False
            
    except Exception as e:
        print(f"Fehler beim Kopieren des Schlüssels: {e}")
        return False

def setup_ha_service():
    print("\n--- Richte High Availability Service ein ---")
    # Dieser Service ruft später ha_manager.py auf (welches wir in Schritt 3 erstellen)
    service_name = "e3dc-ha.service"
    script_path = os.path.join(os.path.dirname(__file__), "ha_manager.py")
    
    # Python venv Pfad ermitteln
    python_exec = "/usr/bin/python3"
    cfg = load_config()
    venv_name = cfg.get("venv_name", ".venv_e3dc")
    if venv_name:
        user = get_install_user()
        home = os.path.expanduser(f"~{user}")
        venv_python = os.path.join(home, venv_name, "bin", "python3")
        if os.path.exists(venv_python):
            python_exec = venv_python
            
    service_content = f"""[Unit]
Description=E3DC-Control High Availability Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart={python_exec} -u {script_path}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=e3dc-ha

[Install]
WantedBy=multi-user.target
"""
    try:
        with open("/tmp/e3dc-ha.service", "w") as f:
            f.write(service_content)
            
        run_command("sudo mv /tmp/e3dc-ha.service /etc/systemd/system/")
        run_command("sudo chmod 644 /etc/systemd/system/e3dc-ha.service")
        run_command("sudo systemctl daemon-reload")
        run_command("sudo systemctl enable e3dc-ha.service")
        run_command("sudo systemctl restart e3dc-ha.service")
        
        print("✓ Service e3dc-ha.service registriert, aktiviert und gestartet.")
        return True
    except Exception as e:
        print(f"Fehler beim Erstellen des Services: {e}")
        return False

def install_ha():
    print("\n" + "="*60)
    print("  High Availability (Cluster) Setup")
    print("="*60 + "\n")
    ha_logger.info("Starte HA Setup")

    user = get_install_user()
    
    print("Dieses Skript richtet die passwortlose Verbindung zum zweiten Raspberry Pi ein")
    print("und registriert den Cluster-Dienst.\n")
    
    print("Welche Rolle soll dieser Raspberry Pi im Cluster einnehmen?")
    print("1 = Master (Aktiv - steuert normalerweise die Anlage)")
    print("2 = Slave  (Standby - übernimmt nur bei Ausfall des Masters)")
    role_choice = input("Auswahl (1/2) [1]: ").strip()
    role = "slave" if role_choice == "2" else "master"

    print()
    peer_ip = validate_peer_ip(input("Wie lautet die IP-Adresse des ANDEREN Raspberry Pi? (z.B. 192.0.2.164): ").strip())
    
    if not peer_ip:
        print("Setup abgebrochen: ungültige Peer-IP.")
        return

    if not setup_ssh_keys(user, peer_ip):
        print("\nSetup abgebrochen, da die SSH-Verbindung nicht hergestellt werden konnte.")
        return

    print("\n→ Speichere Cluster-Konfiguration in e3dc.config.txt...")
    config_path = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    replace_in_file(config_path, "ha_mode", f"ha_mode = {role}")
    replace_in_file(config_path, "ha_peer_ip", f"ha_peer_ip = {peer_ip}")
    replace_in_file(config_path, "ha_fail_timeout", "ha_fail_timeout = 15")
    replace_in_file(config_path, "ha_sync_interval", "ha_sync_interval = 60")
    replace_in_file(config_path, "ha_auto_recover", "ha_auto_recover = 1")
    replace_in_file(config_path, "ha_auto_failover", "ha_auto_failover = 1")
    print("✓ Konfiguration erfolgreich gespeichert.")

    setup_ha_service()
    
    print("\n" + "="*60)
    print("✓ High Availability Setup abgeschlossen!")
    print("="*60)
    print("\nBITTE BEACHTEN:")
    print("1. Führe dieses Setup auch auf dem ANDEREN Raspberry Pi aus,")
    print("   damit die Verbindung in beide Richtungen (für Failback) funktioniert!")
    print(f"   (Wähle dort dann die Rolle: {'Master' if role == 'slave' else 'Slave'})")
    print("2. Der Dienst läuft jetzt im Hintergrund. Du kannst den Status")
    print("   jederzeit im Web-Dashboard (Kopfzeile) überprüfen.")
    print("="*60 + "\n")
    
    log_task_completed("High Availability Setup")

register_command("49", "High Availability (Cluster)", install_ha, sort_order=49)
