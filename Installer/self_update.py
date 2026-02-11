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
from urllib.request import urlopen
from urllib.error import URLError

from .core import register_command
from .utils import run_command

# Repository-Informationen
GITHUB_REPO = "A9xxx/Install-E3DC-Control"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_ROOT = os.path.dirname(SCRIPT_DIR)  # /home/pi
INSTALLER_DIR = os.path.join(INSTALL_ROOT, "Install")
VERSION_FILE = os.path.join(SCRIPT_DIR, "VERSION")


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
        with urlopen(RELEASES_API, timeout=10) as response:
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
    except json.JSONDecodeError:
        print("⚠ Fehler beim Parsen der Release-Informationen")
    except Exception as e:
        print(f"⚠ Fehler beim Abrufen der Release-Informationen: {e}")
    
    return None


def download_release(download_url):
    """
    Lädt die Release-ZIP herunter.
    
    Returns:
        Pfad zur heruntergeladenen Datei oder None bei Fehler
    """
    try:
        print("→ Lade Release herunter…")
        
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, f"E3DC-Install-{os.getpid()}.zip")
        
        with urlopen(download_url, timeout=60) as response:
            with open(zip_path, 'wb') as out_file:
                out_file.write(response.read())
        
        if os.path.exists(zip_path):
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            print(f"✓ Download abgeschlossen ({size_mb:.1f} MB)")
            return zip_path
        
        return None
    
    except URLError as e:
        print(f"✗ Netzwerkfehler beim Download: {e}")
    except Exception as e:
        print(f"✗ Fehler beim Download: {e}")
    
    return None


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
        temp_extract = os.path.join(tempfile.gettempdir(), f"e3dc_update_{os.getpid()}")
        print(f"[DEBUG] Entpacke nach: {temp_extract}")
        # Entpacke ZIP immer aus dem übergebenen Pfad (sollte im Temp liegen)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract)

        # Finde das Installer-Verzeichnis in der ZIP
        extracted_items = os.listdir(temp_extract)
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
            shutil.rmtree(temp_extract, ignore_errors=True)
            return False

        print(f"→ Aktualisiere Installer-Verzeichnis aus: {src_installer}")

        # Sicherung der aktuellen Version (immer!)
        backup_dir = INSTALLER_DIR + ".backup"
        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        if os.path.exists(INSTALLER_DIR):
            try:
                shutil.copytree(INSTALLER_DIR, backup_dir)
                print(f"  → Sicherung erstellt: {backup_dir}")
            except Exception as e:
                print(f"  ⚠ Sicherung fehlgeschlagen: {e}")
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
            
            print("✓ Update erfolgreich installiert")
            
            # Aktualisiere VERSION-Datei mit neuer Version
            try:
                with open(VERSION_FILE, 'w') as f:
                    f.write(new_version)
                print(f"✓ VERSION-Datei aktualisiert: {new_version}")
            except Exception as e:
                print(f"⚠ Konnte VERSION-Datei nicht aktualisieren: {e}")
            
            # Setze Besitzrechte auf pi:pi für den Installationsordner
            try:
                subprocess.run(["chown", "-R", "pi:pi", INSTALLER_DIR], check=True)
                print(f"✓ Rechte für {INSTALLER_DIR} auf pi:pi gesetzt")
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
            # Restore Backup
            if os.path.exists(backup_dir):
                print("→ Stelle Sicherung wieder her…")
                try:
                    if os.path.exists(INSTALLER_DIR):
                        shutil.rmtree(INSTALLER_DIR)
                    shutil.copytree(backup_dir, INSTALLER_DIR)
                    print("✓ Sicherung wiederhergestellt")
                except Exception as restore_e:
                    print(f"✗ Fehler beim Wiederherstellen: {restore_e}")
            return False
    
    except ImportError:
        print("✗ zipfile-Modul nicht verfügbar")
    except Exception as e:
        print(f"✗ Fehler beim Entpacken: {e}")
    
    return False


def check_and_update(silent=False):
    """
    Hauptfunktion: Prüft auf Updates und führt diese durch.
    
    Args:
        silent (bool): Wenn True, nur aktualisieren ohne Nachfragen bei Match
    
    Returns:
        True wenn Update durchgeführt wurde, False sonst
    """
    installed_version = get_installed_version()
    
    if not silent:
        print("\n=== Installer-Update Prüfung ===\n")
        print(f"Installierte Version: {installed_version}")
    
    # Hole neueste Release-Infos
    release_info = get_latest_release_info()
    
    if not release_info:
        if not silent:
            print("⚠ Konnte neueste Version nicht abrufen (möglicherweise keine Internetverbindung).\n")
        return False
    
    latest_version = release_info['version']
    
    if not silent:
        print(f"Neueste Version:      {latest_version}")
    
    # Vergleiche Versionen (einfacher String-Vergleich für Tags)
    if installed_version == latest_version:
        if not silent:
            print("\n✓ Installer ist aktuell.\n")
        return False
    
    # Zeige Release-Notes
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
register_command("0", "Installer aktualisieren", run_self_update_check, sort_order=5)
