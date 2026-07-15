import os
import subprocess
import shutil
import socket

from .core import register_command
from .permissions import run_permissions_wizard
from .utils import install_system_packages
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard

from .ramdisk import setup_ramdisk
from .epex_manager import install_epex_service
from .backup import backup_current_version
from .utils import run_command, cleanup_pycache
from .installer_config import get_install_path, get_user_ids, get_www_data_gid, get_home_dir, load_config, save_config, get_install_user, ensure_web_config
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
    # Muss ganz am Anfang laufen: frische Installationen mit nicht-pi-Usern
    # brauchen e3dc_v4.json/e3dc_paths.json, bevor PHP/Install-Center laden.
    ensure_web_config(get_install_user())
    install_path = get_install_path()

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
    # Hinweis: E3DC-Control (C++) wird in V4 nicht mehr benoetigt.
    # Pycache-Bereinigung des alten Ordners nur wenn er noch existiert.

    print("\n" + "=" * 60)
    print("  ALLES INSTALLIEREN - E3DC-CONTROL KOMPLETT-SETUP")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Systempakete installieren (build-essential, Apache, PHP, etc.)")
    print("  2. E3DC-Control klonen & kompilieren")
    print("  3. Webportal & Diagramm-System einrichten")
    print("  4. E3DC-Konfiguration & Wallbox-Datei erstellen")
    print("  5. Strompreise konfigurieren (optional)")
    print("  6. E3DC-Control Service einrichten (Systemd)")
    print("  7. RAM-Disk einrichten")
    print("  8. Backup der Initialversion erstellen")
    print("  9. Energy Manager einrichten (Luxtronik/IDM Wärmepumpe & Smart Charging)")
    print("  10. EPEX-Manager (SMARD/aWATTar) Service einrichten")
    print("  11. Watchdog & Benachrichtigungs-Dienst installieren (Silent)")
    print("  12. Finale Prüfung & Einrichtung (Berechtigungen, Services etc.)\n")

    # Check auf vorhandene Config im Install-Ordner oder Legacy-Ordner
    possible_configs = [
        os.path.join(get_home_dir(), "Install", "e3dc.config.txt"),
        os.path.join(get_home_dir(), "E3DC-Control", "e3dc.config.txt")
    ]
    possible_config = None
    for p in possible_configs:
        if os.path.exists(p):
            possible_config = p
            break

    use_custom_config = False
    if possible_config:
        print(f"ℹ️  Gefunden: {possible_config}")
        if headless:
            use_custom_config = True
            print("  ✓ Wird im Headless-Modus automatisch verwendet.")
        else:
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
    if os.path.exists(install_path):
        try:
            for item in os.listdir(install_path):
                if item.startswith(".venv") and os.path.isdir(os.path.join(install_path, item)):
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
    if venv_name:
        config['venv_path'] = os.path.join(get_home_dir(), venv_name)
        save_config(config)
    ensure_web_config(get_install_user())
    
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
    if not safe_execute_task("SCHRITT 1/11: Systempakete installieren", install_system_packages, use_venv=use_venv):
        failed_steps.append("Systempakete")

    # =========================================================
    # SCHRITT 2: E3DC-Control Binary (Legacy / optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 2/11: E3DC-Control C++ (Legacy - optional)")
    print("=" * 60)
    print("  [i] Das C++ Binary (Eba-M) wird nicht mehr benötigt.")
    print("       RSCP-Kommunikation erfolgt nativ ueber rscp_client.py")
    cpp_dir = os.path.join(get_home_dir(), "E3DC-Control")
    if os.path.exists(cpp_dir):
        print(f"  [i] Legacy-Verzeichnis gefunden: {cpp_dir} (bleibt unveraendert)")
    else:
        print("  [OK] Schritt übersprungen (Native-Python-Betrieb).\n")

    # =========================================================
    # SCHRITT 3: Web-Portal (PHP)
    # =========================================================
    print("\n" + "=" * 60)
    def install_webportal_and_restart_apache():
        print("-> Installiere Web-Portal (/var/www/html)...")
        installer_dir = os.path.dirname(os.path.abspath(__file__))
        # Suche html/ Verzeichnis: gehe solange hoch bis es gefunden wird
        html_src = None
        search_dir = installer_dir
        for _ in range(5):  # max 5 Ebenen hoch suchen
            candidate = os.path.join(search_dir, 'html')
            if os.path.isdir(candidate):
                html_src = candidate
                break
            search_dir = os.path.dirname(search_dir)
        
        if html_src and os.path.exists(html_src):
            res = run_command(f"sudo rsync -a {html_src}/ /var/www/html/ --exclude 'data' --exclude 'logs' --exclude 'ramdisk' --exclude 'tmp'")
            if res['success']:
                print("  [OK] Web-Portal Dateien erfolgreich kopiert.")
                if not os.path.exists(os.path.join(html_src, "app")):
                    cleanup = run_command("sudo rm -rf /var/www/html/app")
                    if cleanup['success']:
                        print("  [OK] Experimentelle App-Vorschau aus dem Webroot entfernt.")
                    else:
                        print(f"  [!] Experimentelle App-Vorschau konnte nicht entfernt werden: {cleanup['stderr']}")
                
                # Copy release metadata from project root so that UI can read it
                repo_root = os.path.dirname(html_src)
                version_file = os.path.join(repo_root, "VERSION")
                changelog_file = os.path.join(repo_root, "CHANGELOG.md")
                policy_file = os.path.join(repo_root, "UPDATE_POLICY.json")
                
                if os.path.exists(version_file):
                    run_command(f"sudo cp '{version_file}' /var/www/html/VERSION")
                if os.path.exists(changelog_file):
                    run_command(f"sudo cp '{changelog_file}' /var/www/html/CHANGELOG.md")
                if os.path.exists(policy_file):
                    run_command(f"sudo cp '{policy_file}' /var/www/html/UPDATE_POLICY.json")
            else:
                print(f"  [!] Fehler beim Kopieren des Web-Portals: {res['stderr']}")
        else:
            print(f"  [!] Web-Portal Verzeichnis (html/) nicht gefunden ab: {installer_dir}. Uebersprungen.")
            
        if not restart_apache():
            log_warning("install_all", "Apache-Neustart nach Webportal-Installation fehlgeschlagen.")

    if not safe_execute_task("SCHRITT 3/11: Webportal einrichten", install_webportal_and_restart_apache):
        failed_steps.append("Webportal")

    # =========================================================
    # SCHRITT 4: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    def create_configs_task():
        if use_custom_config and possible_config:
            print(f"→ Kopiere vorhandene Konfiguration von {possible_config}...")
            try:
                target_config = os.path.join(install_path, "e3dc.config.txt")
                if possible_config != target_config:
                    shutil.copy2(possible_config, target_config)
                uid, _ = get_user_ids()
                os.chown(target_config, uid, get_www_data_gid())
                os.chmod(target_config, 0o664)
                log_task_completed("Konfiguration kopiert", details=possible_config)
                print("✓ Konfiguration erfolgreich migriert.")
            except Exception as e:
                log_error("install_all", f"Fehler beim Kopieren der Config: {e}", e)
                print(f"✗ Fehler beim Kopieren: {e}")
                create_e3dc_config(headless=headless) # Fallback
        else:
            create_e3dc_config(headless=headless)
            
        # Erstelle leere wallbox.txt nur im Legacy-Modus (wb_native_enable=0)
        # Im Native-Python-Modus wird wallbox.txt NICHT benötigt und darf von Python nicht beschrieben werden!
        # (Regel: Wallbox C++ Fallback ist im Native-Modus verboten)
        import json as _json
        try:
            _v4 = {}
            if os.path.exists(WEB_CONFIG_FILE if 'WEB_CONFIG_FILE' in dir() else '/var/www/html/data/e3dc_v4.json'):
                with open('/var/www/html/data/e3dc_v4.json', 'r') as _f:
                    _v4 = _json.load(_f)
        except Exception: pass
        wb_native = str(_v4.get('wb_native_enable', '1')) == '1'
        
        if not wb_native:
            wallbox_file = os.path.join(install_path, "e3dc.wallbox.txt")
            if not os.path.exists(wallbox_file):
                try:
                    with open(wallbox_file, "w") as f:
                        f.write("# Wallbox Konfiguration (Legacy C++ Modus)\n")
                    uid, _ = get_user_ids()
                    os.chown(wallbox_file, uid, get_www_data_gid())
                    os.chmod(wallbox_file, 0o664)
                    print(f"  [OK] Wallbox-Datei erstellt (Legacy): {wallbox_file}")
                except Exception as e:
                    print(f"  [!] Wallbox-Datei konnte nicht erstellt werden: {e}")
        else:
            print("  [OK] Native Wallbox-Regelung aktiv -- wallbox.txt wird nicht erstellt.")

    if not safe_execute_task("SCHRITT 4/11: E3DC-Konfiguration erstellen", create_configs_task):
        failed_steps.append("Konfiguration")

    # =========================================================
    # SCHRITT 5: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 5/11: Strompreise (optional)")
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
    # SCHRITT 7: RAM-Disk
    # =========================================================
    if not safe_execute_task("SCHRITT 7/11: RAM-Disk einrichten", setup_ramdisk):
        failed_steps.append("RAM-Disk")

    # =========================================================
    # SCHRITT 8: Backup
    # =========================================================
    if not safe_execute_task("SCHRITT 8/11: Backup der Initialversion erstellen", backup_current_version):
        failed_steps.append("Backup")

    # =========================================================
    # SCHRITT 9: Energy Manager
    # ===========    print("\n" + "=" * 60)
    print("SCHRITT 9/11: Energy Manager (Wärmepumpe Luxtronik/IDM & Smart Charging)")
    print("=" * 60)
    em_exists = os.path.exists("/etc/systemd/system/energy_manager.service")
    choice_em = ("j" if em_exists else "n") if headless else input("Energy Manager aktivieren (empfohlen)? (j/n) [j]: ").strip().lower()
    if choice_em != "n":
        from .install_luxtronik import install_luxtronik_menu
        if not safe_execute_task("Energy Manager einrichten", lambda: install_luxtronik_menu(headless=headless)):
            failed_steps.append("Energy Manager")
    else:
        print("-> Uebersprungen.\n")


    # =========================================================
    # SCHRITT 10: EPEX-Manager
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 10/12: EPEX-Manager (SMARD/aWATTar)")
    print("=" * 60)
    choice_epex = "j" if headless else input("EPEX-Manager (fuer dynamische Boersen- / 15m-Tarife) aktivieren? (j/n) [j]: ").strip().lower()
    if choice_epex != "n":
        if not safe_execute_task("EPEX Manager Service installieren", install_epex_service):
            failed_steps.append("EPEX Manager")
        
        # In Zero-Touch mode we also install websocket
        from .utils import setup_websocket_service
        safe_execute_task("Websocket Service installieren", setup_websocket_service)
    else:
        print("-> Uebersprungen.\n")

    # =========================================================
    # SCHRITT 10b: Wallbox Manager
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 10b: Wallbox Manager (Native Python)")
    print("=" * 60)
    wb_exists = os.path.exists("/etc/systemd/system/e3dc-wallbox-manager.service")
    choice_wb = ("j" if wb_exists else "n") if headless else input("Native Wallbox Steuerung aktivieren? (j/n) [j]: ").strip().lower()
    if choice_wb != "n":
        from .install_native_wallbox import setup_wallbox_service
        safe_execute_task("Wallbox Manager installieren", lambda: setup_wallbox_service(headless=headless))
    else:
        print("-> Uebersprungen.\n")

    # =========================================================
    # SCHRITT 11: Watchdog (Silent)
    # =========================================================
    from .install_watchdog import install_watchdog_silent
    if not safe_execute_task("SCHRITT 11/12: Watchdog & Benachrichtigungs-Dienst installieren", install_watchdog_silent):
        failed_steps.append("Watchdog")

    # =========================================================
    # SCHRITT 12: FINALE PRÜFUNG & EINRICHTUNG
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 12/12: Finale Prüfung & Einrichtung (Berechtigungen, Services etc.)")
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
    print("  2. System-Status prüfen:")
    print("     → Öffne das Webportal und navigiere zu 'Service Management', um alle Dienste zu sehen.\n")
    print("  3. Dokumentation:")
    print("     → Weitere Infos findest du im Ordner 'Install/doc' (z.B. zu Watchdog, Venv).\n")


register_command("18", "Alles installieren - E3DC-Control Komplett-Setup (Empfohlen)", install_all_main, sort_order=10)
