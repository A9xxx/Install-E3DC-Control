import os
import subprocess
import time
import sys
import ipaddress
import json
import pwd
import grp

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from .core import register_command
from .utils import _create_service_file
from .installer_config import get_install_user
from .secure_file_transaction import (
    atomic_write_bound_file,
    restore_bound_file,
    snapshot_bound_file,
)
from .ha_writer_admission import (
    instance_role_anchor_matches,
    project_instance_role_anchor,
    transition_instance_role_anchor,
)
from .logging_manager import get_or_create_logger, log_task_completed, log_error

V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
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
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        print("✗ Installationsbenutzer existiert nicht.")
        return False
    home_dir = account.pw_dir
    key_path = os.path.join(home_dir, ".ssh", "id_ed25519")
    
    if not os.path.exists(key_path):
        print("Erstelle neuen SSH-Schlüssel...")
        created = subprocess.run(
            ["sudo", "-u", user, "ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if created.returncode != 0:
            print("✗ SSH-Schlüssel konnte nicht erzeugt werden.")
            return False
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
    try:
        return _create_service_file(
            "e3dc-ha",
            "E3DC-Control High Availability Manager",
            "ha_manager.py",
            restart_sec=10,
            start_service=True,
            enable_service=True,
            restart_policy="always",
            require_venv=True,
            after_services=("network-online.target",),
            wants_services=("network-online.target",),
            syslog_identifier="e3dc-ha",
            service_user="root",
            service_group="root",
        ) is True
    except Exception as e:
        print(f"Fehler beim Erstellen des Services: {e}")
        return False


def _commit_ha_role(role, peer_ip):
    """Bindet V4-Konfiguration, Rollenanker und HA-Unit als eine Transaktion."""

    if os.geteuid() != 0:
        raise PermissionError("HA-Rollenwechsel muss als root ausgeführt werden")
    install_user = get_install_user()
    user_uid = pwd.getpwnam(install_user).pw_uid
    www_data_gid = grp.getgrnam("www-data").gr_gid
    previous = snapshot_bound_file(
        V4_CONFIG_PATH,
        expected_uid=user_uid,
        expected_gid=www_data_gid,
        max_bytes=4 * 1024 * 1024,
    )
    try:
        current = json.loads(bytes(previous["payload"]).decode("utf-8-sig"))
    except (TypeError, UnicodeError, ValueError) as exc:
        raise RuntimeError("V4-Konfiguration ist nicht eindeutig lesbar") from exc
    if not isinstance(current, dict):
        raise RuntimeError("V4-Konfiguration ist kein JSON-Objekt")

    old_mode = str(current.get("ha_mode") or "off").strip().lower()
    if old_mode not in {"off", "master", "slave", "shadow"}:
        raise RuntimeError("Bestehende HA-Rolle ist ungültig")
    old_peer = validate_peer_ip(current.get("ha_peer_ip")) if old_mode in {"master", "slave"} else ""
    if old_mode in {"master", "slave"} and not old_peer:
        raise RuntimeError("Bestehende HA-Rolle besitzt keine gültige Peer-IP")

    # Fehlende Altanker dürfen bei diesem expliziten root-Dialog einmalig aus
    # dem gebundenen Preimage migriert werden. Ein vorhandener Mismatch bleibt
    # ein harter Blocker und wird niemals still überschrieben.
    if not instance_role_anchor_matches(old_mode, peer_ip=old_peer):
        if project_instance_role_anchor(old_mode, peer_ip=old_peer) is not True:
            raise RuntimeError("Bestehender Instanzrollen-Anker widerspricht der Konfiguration")

    updated = dict(current)
    updated.update(
        {
            "ha_mode": role,
            "ha_peer_ip": peer_ip,
            "ha_fail_timeout": "15",
            "ha_sync_interval": "60",
            "ha_auto_recover": "1",
            "ha_auto_failover": "1",
        }
    )
    payload = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    committed = None
    role_changed = old_mode != role or old_peer != peer_ip
    anchor_committed = False
    try:
        committed = atomic_write_bound_file(
            V4_CONFIG_PATH,
            payload,
            uid=int(previous["uid"]),
            gid=int(previous["gid"]),
            mode=int(previous["mode"]),
            expected_snapshot=previous,
            max_existing_bytes=4 * 1024 * 1024,
        )
        if role_changed:
            anchor_committed = transition_instance_role_anchor(
                role,
                peer_ip=peer_ip,
                expected_mode=old_mode,
                expected_peer_ip=old_peer,
            ) is True
            if not anchor_committed:
                raise RuntimeError("Instanzrollen-Anker konnte nicht gebunden gewechselt werden")
        if setup_ha_service() is not True:
            raise RuntimeError("HA-Dienst konnte nicht transaktional installiert werden")
        return True
    except Exception:
        anchor_rollback_ok = True
        if anchor_committed:
            anchor_rollback_ok = transition_instance_role_anchor(
                old_mode,
                peer_ip=old_peer,
                expected_mode=role,
                expected_peer_ip=peer_ip,
            ) is True
        config_rollback_ok = True
        if committed is not None:
            try:
                restore_bound_file(previous, expected_current=committed)
            except Exception:
                config_rollback_ok = False
        if not anchor_rollback_ok or not config_rollback_ok:
            raise RuntimeError(
                "HA-Rollenwechsel fehlgeschlagen; Rückfall blieb unvollständig und alle Writer bleiben gesperrt"
            )
        raise

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
        return False

    if not setup_ssh_keys(user, peer_ip):
        print("\nSetup abgebrochen, da die SSH-Verbindung nicht hergestellt werden konnte.")
        return False

    print("\n→ Binde Cluster-Rolle, V4-Konfiguration und HA-Dienst...")
    try:
        if _commit_ha_role(role, peer_ip) is not True:
            return False
    except Exception as exc:
        print(f"✗ HA-Transaktion fehlgeschlagen: {exc}")
        log_error("HA Setup", f"HA-Transaktion fehlgeschlagen: {exc}", exc)
        return False
    print("✓ HA-Rolle und Dienst sind transaktional bestätigt.")
    
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
    return True

register_command("49", "High Availability (Cluster)", install_ha, sort_order=49)
