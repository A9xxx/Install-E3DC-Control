import os
import subprocess
import shutil
import socket

from .core import register_command
from .permissions import run_permissions_wizard
from .system import install_system_packages, install_e3dc_control
from .diagrammphp import install_diagramm, DiagramInstaller
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard
from .service_setup import install_e3dc_service
from .ramdisk import setup_ramdisk
from .backup import backup_current_version
from .utils import run_command, cleanup_pycache
from .installer_config import get_install_path, get_user_ids, get_www_data_gid, get_home_dir, load_config, save_config
from .logging_manager import setup_installation_loggers, print_installation_summary, log_task_completed, log_error, log_warning
from .task_executor import safe_execute_task

INSTALL_PATH = get_install_path()


def get_ip_address():
    """Holt die lokale IP-Adresse."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Es ist nicht notwendig, eine echte Verbindung herzustellen
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception:
        return "<IP nicht gefunden>"


def restart_apache():
    """Startet Apache2 neu. Gibt True zurück bei Erfolg."""
    print("→ Starte Apache2 neu…")
    try:
        result = run_command("sudo systemctl restart apache2", timeout=10)
        if result['success']:
            print("✓ Apache2 neugestartet\n")
            return True
        else:
            print("⚠ Apache2 Neustart fehlgeschlagen\n")
            return False
    except Exception as e:
        print(f"⚠ Fehler: {e}\n")
        return False


def install_all_main(headless=False):
    """Komplette Installation mit korrekter Reihenfolge."""
    # Cache-Bereinigung vor allen Operationen
    print("\n" + "=" * 60)
    print("  CACHE-BEREINIGUNG")
    print("=" * 60 + "\n")
    
    # Pfade für die Bereinigung definieren
    # Annahme: Dieses Skript liegt in pi/Install/Installer
    installer_dir = os.path.dirname(os.path.abspath(__file__))
    install_dir = os.path.dirname(installer_dir)
    pi_dir = os.path.dirname(install_dir)
    e3dc_control_dir = os.path.join(pi_dir, "E3DC-Control")

    cleanup_pycache(install_dir)
    cleanup_pycache(e3dc_control_dir)

    print("\n" + "=" * 60)
    print("  KOMPLETTE E3DC-CONTROL INSTALLATION")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Systempakete installieren (build-essential, Apache, PHP, etc.)")
    print("  2. E3DC-Control klonen & kompilieren")
    print("  3. Webportal & Diagramm-System einrichten")
    print("  4. E3DC-Konfiguration & Wallbox-Datei erstellen")
    print("  5. Strompreise konfigurieren (optional)")
    print("  6. E3DC-Control Service einrichten (Systemd)")
    print("  7. RAM-Disk & Live-Status-Grabber einrichten")
    print("  8. Backup der Initialversion erstellen")
    print("  9. Watchdog-Dienst installieren (Silent)")
    print("  10. Finale Prüfung & Einrichtung (Berechtigungen, Cronjobs, Services etc.)\n")

    # Check auf vorhandene Config im Install-Ordner
    possible_config = os.path.join(get_home_dir(), "Install", "e3dc.config.txt")
    use_custom_config = False
    if os.path.exists(possible_config) and not headless:
        print(f"ℹ️  Gefunden: {possible_config}")
        if input("  Soll diese Konfigurationsdatei verwendet werden? (j/n): ").strip().lower() == 'j':
            use_custom_config = True
            print("  ✓ Wird in Schritt 4 integriert.")

    # VENV Abfrage
    use_venv = True
    print("\n" + "-" * 60)
    print("PYTHON UMGEBUNG")
    print("-" * 60)

    config = load_config()
    current_venv = config.get("venv_name", ".venv_e3dc")
    
    # Scan nach vorhandenen venvs
    possible_venvs = []
    if os.path.exists(INSTALL_PATH):
        try:
            for item in os.listdir(INSTALL_PATH):
                if item.startswith(".venv") and os.path.isdir(os.path.join(INSTALL_PATH, item)):
                    possible_venvs.append(item)
        except: pass
    
    use_venv = True
    venv_name = current_venv

    if headless:
        # Im Headless-Modus nutzen wir Standardwerte
        use_venv = True
        venv_name = ".venv_e3dc"
        print(f"→ Headless: Nutze Standard-Venv '{venv_name}'")
    else:
        if possible_venvs:
            print(f"Gefundene Umgebungen:")
            for i, v in enumerate(possible_venvs, 1):
                mark = " (aktuell)" if v == current_venv else ""
                print(f"  {i}) {v}{mark}")
            print(f"  n) Neue erstellen / Anderen Namen wählen")
            print(f"  x) Kein venv (System-Python)")
            
            sel = input(f"Auswahl [1]: ").strip().lower()
            if not sel: sel = "1"
            
            if sel == 'x':
                use_venv = False
                venv_name = None
            elif sel == 'n':
                custom = input("Name für neues venv [.venv_e3dc]: ").strip()
                if custom: venv_name = custom
            elif sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(possible_venvs):
                    venv_name = possible_venvs[idx]
        else:
            print("Es wird empfohlen, eine isolierte Python-Umgebung (venv) zu nutzen.")
            sel = input("Soll ein Python venv genutzt werden? (j/n) [j]: ").strip().lower()
            if sel == 'n':
                use_venv = False
                venv_name = None
                print("→ Installation erfolgt systemweit (global).")
            else:
                custom = input("Name für venv [.venv_e3dc]: ").strip()
                if custom: venv_name = custom
                print(f"→ Installation erfolgt im venv ({venv_name}).")

    # Speichern in Config
    config['venv_name'] = venv_name
    save_config(config)
    
    # e3dc_paths.json aktualisieren (für PHP)
    try:
        paths_file = "/var/www/html/e3dc_paths.json"
        if os.path.exists(paths_file):
            import json
            with open(paths_file, 'r') as f:
                d = json.load(f)
            d['venv_name'] = venv_name
            if use_venv and venv_name:
                d['venv_path'] = os.path.join(get_home_dir(), venv_name)
            with open(paths_file, 'w') as f:
                json.dump(d, f, indent=2)
    except: pass

    if not headless:
        confirm = input("\nAlle Schritte ausführen? (j/n): ").strip().lower()
        if confirm != "j":
            print("→ Abgebrochen.\n")
            return

    # Logging für diese "Alles installieren"-Sitzung initialisieren
    setup_installation_loggers()
    failed_steps = []

    # =========================================================
    # SCHRITT 1: Systempakete
    # =========================================================
    print("\n" + "=" * 60)
    if not safe_execute_task("SCHRITT 1/10: Systempakete installieren", install_system_packages, use_venv=use_venv):
        failed_steps.append("Systempakete")

    # =========================================================
    # SCHRITT 2: E3DC-Control Binary
    # =========================================================
    print("\n" + "=" * 60)
    if not safe_execute_task("SCHRITT 2/10: E3DC-Control klonen & kompilieren", install_e3dc_control, headless=headless):
        failed_steps.append("E3DC-Control")
        print("\n✗ Kritischer Fehler: E3DC-Control konnte nicht installiert werden. Installation wird abgebrochen.\n")
        print_installation_summary()
        return

    # =========================================================
    # SCHRITT 3: Diagramm & PHP
    # =========================================================
    print("\n" + "=" * 60)
    def install_diagramm_and_restart_apache():
        # IMMER aus ZIP neu installieren, um sicherzustellen, dass die Web-Dateien frisch sind
        print("→ Erzwinge Neuinstallation des Webportals aus ZIP-Datei...")
        try:
            diag_installer = DiagramInstaller()
            if not diag_installer.extract_and_install_from_zip():
                log_error("install_all", "Neuinstallation des Webportals aus ZIP ist fehlgeschlagen.")
            else:
                log_task_completed("Webportal aus ZIP neu installiert")
        except Exception as e:
            log_error("install_all", f"Fehler bei erzwungener ZIP-Extraktion: {e}", e)

        # Automatische Konfiguration danach anwenden
        install_diagramm(auto_config={'enable_heatpump': True, 'diagram_mode': 'manual'})
        
        if not restart_apache():
            log_warning("install_all", "Apache-Neustart nach Diagramm-Installation fehlgeschlagen.")

    if not safe_execute_task("SCHRITT 3/10: Webportal & Diagramm-System einrichten", install_diagramm_and_restart_apache):
        failed_steps.append("Diagramm")

    # =========================================================
    # SCHRITT 4: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    def create_configs_task():
        if use_custom_config:
            print(f"→ Kopiere vorhandene Konfiguration von {possible_config}...")
            try:
                shutil.copy2(possible_config, os.path.join(INSTALL_PATH, "e3dc.config.txt"))
                uid, _ = get_user_ids()
                os.chown(os.path.join(INSTALL_PATH, "e3dc.config.txt"), uid, get_www_data_gid())
                os.chmod(os.path.join(INSTALL_PATH, "e3dc.config.txt"), 0o664)
                log_task_completed("Konfiguration kopiert", details=possible_config)
                print("✓ Konfiguration kopiert.")
            except Exception as e:
                log_error("install_all", f"Fehler beim Kopieren der Config: {e}", e)
                print(f"✗ Fehler beim Kopieren: {e}")
                create_e3dc_config(headless=headless) # Fallback
        else:
            create_e3dc_config(headless=headless)
            
        # Erstelle auch leere wallbox.txt wenn noch nicht vorhanden
        wallbox_file = os.path.join(INSTALL_PATH, "e3dc.wallbox.txt")
        if not os.path.exists(wallbox_file):
            try:
                with open(wallbox_file, "w") as f:
                    f.write("# Wallbox Konfiguration\n")
                    f.write("# Wird automatisch von der Weboberfläche generiert\n")
                uid, _ = get_user_ids()
                os.chown(wallbox_file, uid, get_www_data_gid())
                os.chmod(wallbox_file, 0o664)
                log_task_completed("Leere Wallbox-Datei erstellt", details=wallbox_file)
                print(f"✓ Wallbox-Datei erstellt: {wallbox_file}\n")
            except Exception as e:
                log_warning("install_all", f"Leere Wallbox-Datei konnte nicht erstellt werden: {e}")
                print(f"⚠ Wallbox-Datei konnte nicht erstellt werden: {e}\n")

    if not safe_execute_task("SCHRITT 4/10: E3DC-Konfiguration erstellen", create_configs_task):
        failed_steps.append("Konfiguration")

    # =========================================================
    # SCHRITT 5: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 5/10: Strompreise (optional)")
    print("=" * 60)
    choice = "n" if headless else input("Strompreise jetzt konfigurieren? (j/n): ").strip().lower()
    if choice == "j" or (headless and False): # Im Headless Modus Strompreise überspringen oder Default? Eher überspringen.
        try:
            strompreis_wizard(headless=headless)
            log_task_completed("Strompreise konfiguriert")
        except Exception as e:
            log_error("Strompreis-Wizard", f"Fehler bei der Strompreis-Konfiguration: {e}", e)
            failed_steps.append("Strompreise")
    else:
        print("→ Übersprungen (kann später hinzugefügt werden).\n")

    # =========================================================
    # SCHRITT 6: Service (Systemd)
    # =========================================================
    if not safe_execute_task("SCHRITT 6/10: E3DC-Control Service einrichten (Systemd)", install_e3dc_service, headless=headless):
        failed_steps.append("Service")

    # =========================================================
    # SCHRITT 7: RAM-Disk & Live-Status
    # =========================================================
    if not safe_execute_task("SCHRITT 7/10: RAM-Disk & Live-Status einrichten", setup_ramdisk):
        failed_steps.append("RAM-Disk")

    # =========================================================
    # SCHRITT 8: Backup
    # =========================================================
    if not safe_execute_task("SCHRITT 8/10: Backup der Initialversion erstellen", backup_current_version):
        failed_steps.append("Backup")

    # =========================================================
    # SCHRITT 9: Watchdog (Silent)
    # =========================================================
    from .install_watchdog import install_watchdog_silent
    if not safe_execute_task("SCHRITT 9/10: Watchdog-Dienst installieren", install_watchdog_silent):
        failed_steps.append("Watchdog")

    # =========================================================
    # SCHRITT 10: FINALE PRÜFUNG & EINRICHTUNG
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 10/10: Finale Prüfung & Einrichtung (Berechtigungen, Cronjobs, Services etc.)")
    print("=" * 60)
    try:
        print("\n→ Führe umfassende Prüfung und Einrichtung des Systems aus…\n")
        run_permissions_wizard(headless=True)
        log_task_completed("Finale Prüfung & Einrichtung", details="run_permissions_wizard(headless=True) ausgeführt.")
    except Exception as e:
        log_error("install_all", f"Fehler bei der finalen Prüfung: {e}", e)
        print(f"✗ Kritischer Fehler bei der finalen Prüfung und Einrichtung: {e}\n")
        failed_steps.append("Finale Prüfung")

    # =========================================================
    # Abschluss + Fehlersammlung
    # =========================================================
    print_installation_summary()

    ip_address = get_ip_address()

    print("Nächste Schritte:")
    print("  1. Webportal öffnen:")
    print(f"     → http://localhost oder http://{ip_address}\n")
    print("  2. E3DC-Control starten:")
    print("     → Mit 'screen -r E3DC' überprüfen (läuft bei Reboot automatisch)\n")
    print("  3. Dokumentation:")
    print("     → Weitere Infos findest du im Ordner 'Install/doc' (z.B. zu Watchdog, Venv).\n")


register_command("18", "Alles installieren", install_all_main, sort_order=180)
