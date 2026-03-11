"""
E3DC-Control Installer - Selbstaktualisierung

Prüft beim Start, ob eine neue Version des Installers auf GitHub verfügbar ist.
"""

import os
import sys
import json
import subprocess
import tempfile
import shutil
from urllib.request import urlopen, Request
from urllib.error import URLError
import re
import time

from .core import register_command
from .utils import run_command, replace_in_file
from .installer_config import get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

# Repository-Informationen
GITHUB_REPO = "A9xxx/Install-E3DC-Control"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER_DIR = SCRIPT_DIR
VERSION_FILE = os.path.join(SCRIPT_DIR, "VERSION")
DEBUG = False
USER_AGENT = "E3DC-Control-Installer/1.0"
update_logger = get_or_create_logger("self_update")


def get_installed_version():
    """
    Holt die aktuelle Version des Installers.
    
    Versucht zunächst, die VERSION-Datei zu lesen.
    Falls nicht vorhanden, nutzt Git-Commit.
    """
    # Versuche VERSION-Datei zu lesen
    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r") as f:
                version = f.read().strip()
                if version:
                    return version
        except Exception:
            pass
    
    # Fallback: Git-Commit
    try:
        result = run_command(f"cd {SCRIPT_DIR} && git rev-parse --short HEAD", timeout=5)
        if result['success'] and result['stdout'].strip():
            return result['stdout'].strip()
    except Exception:
        pass
    
    return "unknown"


def get_latest_release_info():
    """
    Holt Informationen über das neueste Release von GitHub.
    
    Returns:
        dict mit 'version', 'download_url', 'body' oder None bei Fehler
    """
    try:
        request = Request(RELEASES_API, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode())
            
            # Prüfe ob es ein Release ist (nicht nur Draft/Prerelease)
            if data.get('draft'):
                return None
            
            release_info = {
                'version': data.get('tag_name', 'unknown').lstrip('v'),
                'prerelease': data.get('prerelease', False),
                'body': data.get('body', ''),
                'download_url': None,
                'assets': []
            }
            
            # Finde ZIP-Asset
            for asset in data.get('assets', []):
                name = asset.get('name', '').lower()
                if '.zip' in name and 'source' not in name:
                    release_info['download_url'] = asset.get('browser_download_url')
                    release_info['assets'].append({
                        'name': asset.get('name'),
                        'url': asset.get('browser_download_url'),
                        'size': asset.get('size')
                    })
            
            return release_info if release_info['download_url'] else None
    
    except URLError as e:
        print(f"⚠ Netzwerkfehler beim Abrufen der Release-Informationen: {e}")
        log_warning("self_update", f"Netzwerkfehler beim Abrufen der Release-Informationen: {e}")
    except json.JSONDecodeError:
        print("⚠ Fehler beim Parsen der Release-Informationen")
        log_warning("self_update", "Fehler beim Parsen der Release-Informationen")
    except Exception as e:
        print(f"⚠ Fehler beim Abrufen der Release-Informationen: {e}")
        log_warning("self_update", f"Fehler beim Abrufen der Release-Informationen: {e}")
    
    return None


def download_release(download_url):
    """
    Lädt die Release-ZIP herunter.
    
    Returns:
        Pfad zur heruntergeladenen Datei oder None bei Fehler
    """
    try:
        print("→ Lade Release herunter…")
        update_logger.info(f"Starte Download von: {download_url}")
        
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, f"E3DC-Install-{os.getpid()}.zip")
        
        request = Request(download_url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=60) as response:
            with open(zip_path, 'wb') as out_file:
                out_file.write(response.read())
        
        if os.path.exists(zip_path):
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            print(f"✓ Download abgeschlossen ({size_mb:.1f} MB)")
            update_logger.info(f"Download abgeschlossen: {size_mb:.1f} MB")
            return zip_path
        
        return None
    
    except URLError as e:
        print(f"✗ Netzwerkfehler beim Download: {e}")
        log_error("self_update", f"Netzwerkfehler beim Download: {e}", e)
    except Exception as e:
        print(f"✗ Fehler beim Download: {e}")
        log_error("self_update", f"Fehler beim Download: {e}", e)
    
    return None


def _run_migration_luxtronik_config(installer_dir):
    """
    Führt eine einmalige Migration von der alten config.lux.json
    zur zentralen e3dc.config.txt durch.
    """
    from .installer_config import get_install_path
    lux_config_path = os.path.join(installer_dir, "Installer", "luxtronik", "config.lux.json")
    e3dc_config_path = os.path.join(get_install_path(), "e3dc.config.txt")

    if not os.path.exists(lux_config_path):
        return # Nichts zu migrieren

    print("→ Führe Konfigurations-Migration für Luxtronik durch...")
    update_logger.info("Starte Migration von config.lux.json...")
    try:
        with open(lux_config_path, 'r', encoding='utf-8') as f:
            lux_conf = json.load(f)

        with open(e3dc_config_path, 'r', encoding='utf-8') as f:
            e3dc_lines = f.readlines()

        new_lines = []
        keys_to_update = {k.lower(): str(v) for k, v in lux_conf.items()}
        
        for line in e3dc_lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#'):
                new_lines.append(line)
                continue
            
            if '=' in stripped_line:
                key, _ = stripped_line.split('=', 1)
                key_lower = key.strip().lower()
                if key_lower in keys_to_update:
                    new_lines.append(f"{key.strip()} = {keys_to_update[key_lower]}\n")
                    del keys_to_update[key_lower]
                    continue
            new_lines.append(line)

        if keys_to_update:
            new_lines.append("\n# --- Automatisch migrierte Luxtronik-Parameter ---\n")
            for key, value in keys_to_update.items():
                new_lines.append(f"{key} = {value}\n")

        with open(e3dc_config_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        os.rename(lux_config_path, lux_config_path + ".migrated")
        print("✓ Konfiguration erfolgreich nach e3dc.config.txt migriert.")
        update_logger.info("Luxtronik-Konfiguration erfolgreich migriert.")

    except Exception as e:
        print(f"⚠ Fehler bei der Konfigurations-Migration: {e}")
        update_logger.error(f"Fehler bei der Konfigurations-Migration: {e}", e)

def extract_release(zip_path, new_version):
    """
    Entpackt die Release-ZIP und führt Update durch.
    
    Args:
        zip_path: Pfad zur heruntergeladenen ZIP-Datei
        new_version: Die neue Version, die in VERSION-Datei geschrieben wird
    
    Returns:
        True bei Erfolg, False bei Fehler
    """
    try:
        import zipfile
        print("→ Entpacke Update…")
        update_logger.info("Entpacke Update-ZIP...")
        temp_extract = os.path.join(tempfile.gettempdir(), f"e3dc_update_{os.getpid()}")
        if DEBUG:
            print(f"[DEBUG] Entpacke nach: {temp_extract}")
        # Entpacke ZIP immer aus dem übergebenen Pfad (sollte im Temp liegen)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract)

        # Finde das Installer-Verzeichnis in der ZIP
        extracted_items = os.listdir(temp_extract)
        if DEBUG:
            print(f"[DEBUG] ZIP-Inhalt: {extracted_items}")
        src_installer = None
        # Suche nach dem Install-Ordner in typischer ZIP-Struktur
        for item in extracted_items:
            item_path = os.path.join(temp_extract, item)
            if os.path.isdir(item_path):
                # Prüfe, ob darin ein Install-Ordner liegt
                possible = os.path.join(item_path, "Install")
                if os.path.exists(possible):
                    src_installer = possible
                    break
        # Fallback: Direkt im temp_extract
        if not src_installer and os.path.exists(os.path.join(temp_extract, "Install")):
            src_installer = os.path.join(temp_extract, "Install")

        if not src_installer or not os.path.exists(src_installer):
            print(f"✗ Installer-Verzeichnis nicht in ZIP gefunden: {src_installer}")
            log_error("self_update", "Installer-Verzeichnis nicht in ZIP gefunden.")
            shutil.rmtree(temp_extract, ignore_errors=True)
            return False

        print(f"→ Aktualisiere Installer-Verzeichnis aus: {src_installer}")
        update_logger.info(f"Aktualisiere Installer-Verzeichnis aus: {src_installer}")
        print(f"  Ziel-Verzeichnis (wird aktualisiert): {INSTALLER_DIR}")

        # Sicherung der Konfiguration
        config_to_preserve = None
        config_path = os.path.join(INSTALLER_DIR, "Installer", "installer_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_to_preserve = f.read()
                print("  → Sichern der installer_config.json")
            except Exception as e:
                print(f"  ⚠ Sicherung der installer_config.json fehlgeschlagen: {e}")

        # Sicherung der aktuellen Version (immer!)
        backup_dir = INSTALLER_DIR + ".backup"
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        if os.path.exists(INSTALLER_DIR):
            try:
                shutil.copytree(INSTALLER_DIR, backup_dir)
                print(f"  → Sicherung erstellt: {backup_dir}")
                update_logger.info(f"Backup erstellt: {backup_dir}")
            except Exception as e:
                print(f"  ⚠ Sicherung fehlgeschlagen: {e}")
                log_warning("self_update", f"Backup vor Update fehlgeschlagen: {e}")
        else:
            print(f"  ⚠ Kein bestehendes Installationsverzeichnis für Backup gefunden: {INSTALLER_DIR}")

        # Ersetze Installer-Verzeichnis (Inhalt, nicht das Verzeichnis selbst)
        try:
            # Lösche nur den Inhalt, nicht das Verzeichnis selbst (Shell-Kompatibilität)
            if os.path.exists(INSTALLER_DIR):
                for item in os.listdir(INSTALLER_DIR):
                    item_path = os.path.join(INSTALLER_DIR, item)
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)
            else:
                os.makedirs(INSTALLER_DIR)
            
            # Kopiere neuen Inhalt ins bestehende Verzeichnis
            for item in os.listdir(src_installer):
                src_path = os.path.join(src_installer, item)
                dst_path = os.path.join(INSTALLER_DIR, item)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)
            
            # --- UPDATE POLICY & ACTIONS ---
            # Prüfen auf UPDATE_POLICY.json im Quellpaket
            policy_file = os.path.join(src_installer, "..", "UPDATE_POLICY.json")
            policy_executed = False
            
            # Standard-Aktion: Energy Manager neu starten, falls keine Policy das Gegenteil sagt
            services_to_restart = ["energy_manager"]
            run_full_permissions = False
            apt_packages = []
            pip_packages = []

            if os.path.exists(policy_file):
                try:
                    print("→ Verarbeite Update-Richtlinien (Policy)...")
                    with open(policy_file, 'r') as f:
                        policy = json.load(f)
                        if "restart_services" in policy:
                            services_to_restart = policy["restart_services"]
                        if "run_permissions" in policy:
                            run_full_permissions = bool(policy["run_permissions"])
                        if "apt_packages" in policy:
                            apt_packages = policy.get("apt_packages", [])
                        if "pip_packages" in policy:
                            pip_packages = policy.get("pip_packages", [])
                            
                        # Hier könnten in Zukunft auch Migrations-Skripte ausgeführt werden
                        policy_executed = True
                except Exception as pe:
                    print(f"⚠ Fehler beim Lesen der Update-Policy: {pe}")

            # Migration der Luxtronik-Konfiguration nach dem Kopieren der neuen Dateien
            _run_migration_luxtronik_config(INSTALLER_DIR)

            # 1. Pakete nachinstallieren (falls angefordert)
            if apt_packages:
                print(f"→ Installiere System-Pakete: {', '.join(apt_packages)}")
                try:
                    subprocess.run(["apt-get", "update"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    subprocess.run(["apt-get", "install", "-y"] + apt_packages, check=True)
                except Exception as e:
                    print(f"⚠ Fehler bei der Paket-Installation (apt): {e}")
            
            if pip_packages:
                print(f"→ Installiere Python-Pakete: {', '.join(pip_packages)}")
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install"] + pip_packages, check=True)
                except Exception as e:
                    print(f"⚠ Fehler bei der Paket-Installation (pip): {e}")

            # 2. Rechte korrigieren (Vollständiger Wizard, falls angefordert)
            if run_full_permissions:
                print("→ Führe vollständige Rechte-Reparatur aus (Policy)...")
                try:
                    from .permissions import run_permissions_wizard
                    run_permissions_wizard(headless=True)
                except Exception as e:
                    print(f"⚠ Fehler bei der Rechte-Reparatur: {e}")

            # Dienste neustarten (Standard oder Policy)
            for srv in services_to_restart:
                if os.path.exists(f"/etc/systemd/system/{srv}.service"):
                    print(f"  → Starte Service neu: {srv}")
                    subprocess.run(["systemctl", "restart", srv], check=False)

            print("✓ Update erfolgreich installiert")
            update_logger.info("Dateien erfolgreich aktualisiert.")

            # Wiederherstellen der Konfiguration
            if config_to_preserve:
                try:
                    # Ensure the "Installer" directory exists
                    os.makedirs(os.path.dirname(config_path), exist_ok=True)
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.write(config_to_preserve)
                    print("✓ Wiederherstellen der installer_config.json")
                except Exception as e:
                    print(f"⚠ Wiederherstellen der installer_config.json fehlgeschlagen: {e}")
            
            # Aktualisiere VERSION-Datei mit neuer Version
            try:
                with open(VERSION_FILE, 'w') as f:
                    f.write(new_version)
                print(f"✓ VERSION-Datei aktualisiert: {new_version}")
            except Exception as e:
                print(f"⚠ Konnte VERSION-Datei nicht aktualisieren: {e}")
            
            # Benachrichtigung für Web-Interface erstellen
            try:
                note_file = "/var/www/html/ramdisk/update_completed.json"
                with open(note_file, 'w') as f:
                    json.dump({"ts": time.time(), "version": new_version, "status": "success"}, f)
                os.chmod(note_file, 0o666)
            except: pass

            # Setze Besitzrechte für den Installationsordner
            try:
                install_user = get_install_user()
                subprocess.run(["chown", "-R", f"{install_user}:{install_user}", INSTALLER_DIR], check=True)
                print(f"✓ Rechte für {INSTALLER_DIR} auf {install_user}:{install_user} gesetzt")
            except Exception as e:
                print(f"⚠ Konnte Rechte nicht setzen: {e}")
            # Entferne alte Sicherung
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir, ignore_errors=True)
            # Cleanup
            shutil.rmtree(temp_extract, ignore_errors=True)
            if os.path.exists(zip_path):
                os.remove(zip_path)
            return True
        except Exception as e:
            print(f"✗ Fehler beim Verschieben der Dateien: {e}")
            log_error("self_update", f"Fehler beim Verschieben der Dateien: {e}", e)
            # Restore Backup
            if os.path.exists(backup_dir):
                print("→ Stelle Sicherung wieder her…")
                try:
                    if os.path.exists(INSTALLER_DIR):
                        shutil.rmtree(INSTALLER_DIR)
                    shutil.copytree(backup_dir, INSTALLER_DIR)
                    update_logger.info("Sicherung nach Fehler wiederhergestellt.")
                    print("✓ Sicherung wiederhergestellt")
                except Exception as restore_e:
                    print(f"✗ Fehler beim Wiederherstellen: {restore_e}")
                    log_error("self_update", f"Fehler beim Wiederherstellen der Sicherung: {restore_e}", restore_e)
            return False
    
    except ImportError:
        print("✗ zipfile-Modul nicht verfügbar")
        log_error("self_update", "zipfile-Modul nicht verfügbar.")
    except Exception as e:
        print(f"✗ Fehler beim Entpacken: {e}")
        log_error("self_update", f"Fehler beim Entpacken: {e}", e)
    
    return False


def is_newer_version(latest, installed):
    def parse(v):
        parts = [p for p in re.split(r'[^0-9]+', v) if p]
        return [int(p) for p in parts] if parts else []
    lv = parse(latest)
    iv = parse(installed)
    if not lv or not iv:
        return latest != installed
    for i in range(max(len(lv), len(iv))):
        l = lv[i] if i < len(lv) else 0
        r = iv[i] if i < len(iv) else 0
        if l > r:
            return True
        if l < r:
            return False
    return False


def check_and_update(silent=False, check_only=False):
    """
    Hauptfunktion: Prüft auf Updates und führt diese durch.
    
    Args:
        silent (bool): Wenn True, nur aktualisieren ohne Nachfragen bei Match
        check_only (bool): Wenn True, nur prüfen und True/False zurückgeben (keine Installation)
    
    Returns:
        True wenn Update durchgeführt wurde, False sonst
    """
    installed_version = get_installed_version()
    
    if not silent:
        print("\n=== Installer-Update Prüfung ===\n")
        print(f"Installierte Version: {installed_version}")
        update_logger.info(f"Prüfe auf Updates. Installiert: {installed_version}")
    
    # Hole neueste Release-Infos
    release_info = get_latest_release_info()
    
    if not release_info:
        if not silent:
            print("⚠ Konnte neueste Version nicht abrufen (möglicherweise keine Internetverbindung).\n")
        return False
    
    latest_version = release_info['version']
    
    if not silent:
        print(f"Neueste Version:      {latest_version}")
    
    if not is_newer_version(latest_version, installed_version):
        if not silent:
            print("\n✓ Installer ist aktuell.\n")
            update_logger.info("Installer ist aktuell.")
            
            if latest_version == installed_version:
                if input("Möchtest du das Update trotzdem erzwingen (Re-Install)? (j/n): ").strip().lower() != 'j':
                    return False
            else:
                return False
        else:
            return False

    if check_only:
        return True
    
    # Zeige Release-Notes
    if not silent:
        if latest_version == installed_version:
            print(f"\n→ Re-Installation von Version {latest_version}...\n")
        else:
            print(f"\n→ Neue Version verfügbar!\n")

        if release_info['body']:
            print("Änderungen:")
            print("-" * 40)
            # Begrenzte Länge für Anzeige
            body = release_info['body'][:500]
            print(body)
            if len(release_info['body']) > 500:
                print("\n... (weitere Informationen auf GitHub)")
            print("-" * 40 + "\n")

        # Frage Benutzer
        choice = input("Soll die neue Version jetzt installiert werden? (j/n): ").strip().lower()

        if choice != "j":
            print("→ Update übersprungen.\n")
            update_logger.info("Update vom Benutzer abgebrochen.")
            return False
    
    # Lade Release herunter
    print()
    zip_path = download_release(release_info['download_url'])
    if not zip_path:
        print("✗ Download fehlgeschlagen.\n")
        return False
    
    # Entpacke und installiere
    print()
    if not extract_release(zip_path, release_info['version']):
        print("✗ Installation fehlgeschlagen.\n")
        return False
    
    print("\n→ Installer wird neu gestartet…\n")
    log_task_completed("Installer aktualisiert", details=f"Auf Version {latest_version}")
    # Ersetze aktuellen Prozess durch neu gestarteten Installer (behält sudo-Rechte und Terminal)
    installer_main = os.path.join(INSTALLER_DIR, 'installer_main.py')
    os.execv(sys.executable, [sys.executable, installer_main])


def run_self_update_check():
    """
    API für Menu-Integration.
    Startet Update-Check und bietet Menü an.
    """
    print()
    check_and_update(silent=False)


# Registriere als Menü-Befehl
register_command("1", "Installer aktualisieren", run_self_update_check, sort_order=10)
