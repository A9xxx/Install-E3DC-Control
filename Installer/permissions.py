import os
import pwd
import grp
import subprocess
import logging

from .core import register_command
from .utils import run_command, setup_logging
from .installer_config import get_install_path, get_install_user, get_home_dir, get_www_data_gid
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_USER = get_install_user()
INSTALL_HOME = get_home_dir(INSTALL_USER)
INSTALL_PATH = get_install_path()


def setup_permissions_logging():
    """Initialisiert Logging für Berechtigungen über logging_manager."""
    import os
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(os.path.dirname(script_dir), "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, "permissions.log")
    perm_logger = get_or_create_logger("permissions", log_file)
    
    return perm_logger


perm_logger = setup_permissions_logging()


def check_permissions():
    """Prüft Installation-Verzeichnis."""
    print("\n=== Verzeichnis-Rechteprüfung ===\n")
    perm_logger.info("--- Starte Verzeichnis-Rechteprüfung ---")


    def format_dir_issue(owner, group, mode, expected_owner, expected_group, expected_mode):
        details = []
        if owner != expected_owner:
            details.append(f"Owner={owner} (soll: {expected_owner})")
        if group != expected_group:
            details.append(f"Gruppe={group} (soll: {expected_group})")
        if mode != expected_mode:
            details.append(f"Modus={mode} (soll: {expected_mode})")
        return ", ".join(details) if details else "unbekannte Abweichung"

    issues = []

    # Home-Verzeichnis muss für www-data betretbar sein (execute-bit)
    try:
        st_home = os.stat(INSTALL_HOME)
        www_data_gid = get_www_data_gid()
        other_x = bool(st_home.st_mode & 0o001)
        group_x = st_home.st_gid == www_data_gid and bool(st_home.st_mode & 0o010)

        if not other_x and not group_x:
            print(f"✗ {INSTALL_HOME} ist NICHT für www-data erreichbar")
            perm_logger.error(f"Home-Verzeichnis nicht für www-data erreichbar: {INSTALL_HOME}")
            issues.append("home")
        else:
            print(f"✓ {INSTALL_HOME} ist für www-data erreichbar")
            perm_logger.info(f"Home-Verzeichnis OK: {INSTALL_HOME}")
    except Exception as e:
        print(f"✗ Fehler beim Prüfen von {INSTALL_HOME}: {e}")
        perm_logger.error(f"Fehler beim Prüfen von {INSTALL_HOME}: {e}")
        issues.append("home")

    # INSTALL_PATH prüfen
    if not os.path.exists(INSTALL_PATH):
        print(f"✓ {INSTALL_PATH} existiert noch nicht – überspringe Rechteprüfung.\n")
        perm_logger.info(f"Install-Pfad existiert noch nicht: {INSTALL_PATH}")
        return issues

    if not os.path.isdir(INSTALL_PATH):
        print(f"✗ {INSTALL_PATH} ist kein Verzeichnis!")
        perm_logger.error(f"Install-Pfad ist kein Verzeichnis: {INSTALL_PATH}")
        issues.append("notdir")
        return issues

    try:
        st = os.stat(INSTALL_PATH)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = oct(st.st_mode)[-3:]

        if owner != INSTALL_USER or group != INSTALL_USER:
            details = format_dir_issue(owner, group, mode, INSTALL_USER, INSTALL_USER, "755")
            print(f"✗ {INSTALL_PATH} Problem: {details}")
            perm_logger.error(f"INSTALL_PATH Besitzer/Gruppe falsch: {details}")
            issues.append("owner")
        else:
            print(f"✓ {INSTALL_PATH} gehört {INSTALL_USER}:{INSTALL_USER}")
            perm_logger.info(f"INSTALL_PATH Besitzer OK: {INSTALL_USER}:{INSTALL_USER}")

        if mode != "755":
            print(f"✗ {INSTALL_PATH} hat Rechte {mode} statt 755")
            perm_logger.error(f"INSTALL_PATH Modus falsch: {mode} (soll: 755)")
            issues.append("mode")
        else:
            print(f"✓ {INSTALL_PATH} hat korrekte Rechte (755)")
            perm_logger.info(f"INSTALL_PATH Modus OK: 755")
    except Exception as e:
        print(f"✗ Fehler beim Prüfen: {e}")
        perm_logger.error(f"Fehler beim Prüfen von INSTALL_PATH: {e}")
        issues.append("error")

    return issues


def check_webportal_permissions():
    """Prüft Webportal-Verzeichnis."""
    print("\n=== Webportal-Rechteprüfung ===\n")
    perm_logger.info("--- Starte Webportal-Rechteprüfung ---")

    def format_wp_issue(owner, group, mode, expected_owner, expected_group, expected_mode):
        details = []
        if owner != expected_owner:
            details.append(f"Owner={owner} (soll: {expected_owner})")
        if group != expected_group:
            details.append(f"Gruppe={group} (soll: {expected_group})")
        if mode != expected_mode:
            details.append(f"Modus={mode} (soll: {expected_mode})")
        return ", ".join(details) if details else "unbekannte Abweichung"

    issues = []
    wp_path = "/var/www/html"

    if not os.path.exists(wp_path):
        print(f"✗ {wp_path} existiert nicht – Webportal nicht installiert")
        issues.append("wp_missing")
        return issues

    try:
        st = os.stat(wp_path)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        mode = oct(st.st_mode)[-3:]

        if owner != INSTALL_USER or group != "www-data":
            details = format_wp_issue(owner, group, mode, INSTALL_USER, "www-data", "775")
            print(f"✗ {wp_path} Problem: {details}")
            issues.append("wp_owner")
        else:
            print(f"✓ {wp_path} gehört {INSTALL_USER}:www-data")

        if mode != "775":
            print(f"✗ {wp_path} hat Rechte {mode} statt 775")
            issues.append("wp_mode")
        else:
            print(f"✓ {wp_path} hat korrekte Rechte (775)")

        # Sub-Ordner prüfen
        subfolders = [
            (f"{wp_path}/tmp", "777"),
            (f"{wp_path}/icons", "775"),
            (f"{wp_path}/ramdisk", "775")
        ]

        for folder_path, expected_mode in subfolders:
            if not os.path.exists(folder_path):
                print(f"✗ {folder_path} existiert nicht")
                issues.append(f"{os.path.basename(folder_path)}_missing")
            else:
                st_sub = os.stat(folder_path)
                mode_sub = oct(st_sub.st_mode)[-3:]
                owner_sub = pwd.getpwuid(st_sub.st_uid).pw_name
                group_sub = grp.getgrgid(st_sub.st_gid).gr_name

                # Prüfe Owner/Group separat
                owner_group_issue = owner_sub != INSTALL_USER or group_sub != "www-data"
                mode_issue = mode_sub != expected_mode

                if owner_group_issue or mode_issue:
                    details = format_wp_issue(owner_sub, group_sub, mode_sub, INSTALL_USER, "www-data", expected_mode)
                    print(f"✗ {folder_path} Problem: {details}")
                    
                    # Separate Issue-Keys für Owner und Mode
                    folder_name = os.path.basename(folder_path)
                    if owner_group_issue:
                        issues.append(f"{folder_name}_owner")
                    if mode_issue:
                        issues.append(f"{folder_name}_mode")
                else:
                    print(f"✓ {folder_path} OK ({INSTALL_USER}:www-data, {expected_mode})")

                # tmp-Ordner Schreibprüfung für www-data
                if os.path.basename(folder_path) == "tmp":
                    www_data_gid = get_www_data_gid()
                    other_wx = bool(st_sub.st_mode & 0o003)
                    group_wx = st_sub.st_gid == www_data_gid and bool(st_sub.st_mode & 0o030)
                    if not other_wx and not group_wx:
                        print(f"✗ {folder_path} ist für www-data nicht schreibbar")
                        issues.append("tmp_not_writable")
                    else:
                        print(f"✓ {folder_path} ist für www-data schreibbar")

    except Exception as e:
        print(f"✗ Fehler beim Prüfen: {e}")
        issues.append("error")

    return issues


def check_file_permissions():
    """Prüft Dateien, die PHP schreiben muss (config/wallbox) und Python-Dateien."""
    print("\n=== Datei-Rechteprüfung ===\n")

    def build_issue_details(owner, group, mode, expected_mode):
        details = []
        if owner != INSTALL_USER:
            details.append(f"Owner={owner} (soll: {INSTALL_USER})")
        if group != "www-data":
            details.append(f"Gruppe={group} (soll: www-data)")
        if mode != expected_mode:
            details.append(f"Modus={mode} (soll: {expected_mode})")
        return ", ".join(details) if details else "unbekannte Abweichung"

    # Konfigurationsdateien (pi:www-data mit 664)
    config_files = [
        (f"{INSTALL_PATH}/e3dc.config.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.wallbox.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.strompreis.txt", "664")  # Strompreise
    ]
    
    # Ausführbare Python-Dateien (pi:www-data mit 755)
    executable_files = [
        (f"{INSTALL_PATH}/plot_soc_changes.py", "755"),
        (f"{INSTALL_HOME}/get_live.sh", "755")
    ]

    # Web-Ausgabedateien (pi:www-data mit 664)
    web_files = [
        ("/var/www/html/diagramm.html", "664"),
        ("/var/www/html/archiv_diagramm.html", "664"),
        ("/var/www/html/diagramm_mobile.html", "664"),
        ("/var/www/html/sw.js", "664"),
        ("/var/www/html/manifest.json", "664"),
        ("/var/www/html/mobile.php", "664"),
        ("/var/www/html/status.php", "664"),
        ("/var/www/html/get_live_json.php", "664"),
        ("/var/www/html/ramdisk/live.txt", "664", True),
        ("/var/www/html/tmp/plot_soc_done", "666"),
        ("/var/www/html/tmp/plot_soc_done_archiv", "666"),
        ("/var/www/html/tmp/plot_soc_error", "664", True),
        ("/var/www/html/tmp/plot_soc_error_archiv", "664", True),
        ("/var/www/html/tmp/plot_soc_last_run", "666")
    ]
    
    issues = {}
    
    # Prüfe Konfigurationsdateien (pi:www-data mit 664)
    for path, expected_mode in config_files:
        if not os.path.exists(path):
            continue
        
        try:
            st = os.stat(path)
            mode = oct(st.st_mode)[-3:]
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            
            status_ok = owner == INSTALL_USER and group == "www-data" and mode == expected_mode
            
            if status_ok:
                print(f"✓ {os.path.basename(path)} OK ({INSTALL_USER}:www-data, {expected_mode})")
            else:
                details = build_issue_details(owner, group, mode, expected_mode)
                print(f"✗ {os.path.basename(path)} Problem: {details}")
                issues[path] = {
                    "owner": owner != INSTALL_USER,
                    "group": group != "www-data",
                    "mode": mode != expected_mode,
                    "expected_mode": expected_mode
                }
        except Exception as e:
            print(f"✗ Fehler bei {path}: {e}")
    
    # Prüfe ausführbare Python-Dateien (pi:www-data mit 755)
    for path, expected_mode in executable_files:
        if not os.path.exists(path):
            continue
        
        try:
            st = os.stat(path)
            mode = oct(st.st_mode)[-3:]
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            
            is_executable = bool(st.st_mode & 0o111)
            status_ok = owner == INSTALL_USER and group == "www-data" and mode == expected_mode and is_executable
            
            if status_ok:
                print(f"✓ {os.path.basename(path)} OK ({INSTALL_USER}:www-data, {expected_mode}, ausführbar)")
            else:
                details = build_issue_details(owner, group, mode, expected_mode)
                if not is_executable:
                    details = f"{details}, nicht ausführbar"
                print(f"✗ {os.path.basename(path)} Problem: {details}")
                issues[path] = {
                    "owner": owner != INSTALL_USER,
                    "group": group != "www-data",
                    "mode": mode != expected_mode,
                    "expected_mode": expected_mode
                }
        except Exception as e:
            print(f"✗ Fehler bei {path}: {e}")

    # Prüfe Web-Ausgabedateien (pi:www-data mit 664)
    for entry in web_files:
        if len(entry) == 3:
            path, expected_mode, allow_missing = entry
        else:
            path, expected_mode = entry
            allow_missing = False
        if not os.path.exists(path):
            if not allow_missing:
                print(f"✗ {os.path.basename(path)} fehlt")
                issues[path] = {
                    "missing": True,
                    "expected_mode": expected_mode
                }
            continue

        try:
            st = os.stat(path)
            mode = oct(st.st_mode)[-3:]
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name

            status_ok = owner == INSTALL_USER and group == "www-data" and mode == expected_mode

            if status_ok:
                print(f"✓ {os.path.basename(path)} OK ({INSTALL_USER}:www-data, {expected_mode})")
            else:
                details = build_issue_details(owner, group, mode, expected_mode)
                print(f"✗ {os.path.basename(path)} Problem: {details}")
                issues[path] = {
                    "owner": owner != INSTALL_USER,
                    "group": group != "www-data",
                    "mode": mode != expected_mode,
                    "expected_mode": expected_mode
                }
        except Exception as e:
            print(f"✗ Fehler bei {path}: {e}")
    
    return issues


def fix_permissions(issues):
    """Korrigiert Installation-Verzeichnis-Rechte."""
    print("\n→ Korrigiere Verzeichnis-Berechtigungen…\n")

    success = True

    if "home" in issues:
        print(f"  → Setze Execute-Bit auf Home-Verzeichnis: {INSTALL_HOME} (soll: für www-data betretbar)")
        result = run_command(f"sudo chmod o+x {INSTALL_HOME}")
        if result['success']:
            print(f"✓ {INSTALL_HOME}: Execute-Bit für Others gesetzt")
        else:
            success = False

    if "owner" in issues:
        print(f"  → Setze Besitzer rekursiv: {INSTALL_PATH} -> {INSTALL_USER}:{INSTALL_USER}")
        result = run_command(f"sudo chown -R {INSTALL_USER}:{INSTALL_USER} {INSTALL_PATH}")
        if result['success']:
            print(f"✓ {INSTALL_PATH}: Besitzer auf {INSTALL_USER}:{INSTALL_USER} gesetzt")
        else:
            success = False

    if "mode" in issues:
        print(f"  → Setze Verzeichnis-/Dateirechte rekursiv: {INSTALL_PATH} -> 755")
        result = run_command(f"sudo chmod -R 755 {INSTALL_PATH}")
        if result['success']:
            print(f"✓ {INSTALL_PATH}: Rechte auf 755 gesetzt")
        else:
            success = False

    if "notdir" in issues:
        print(f"✗ {INSTALL_PATH} ist keine Ordnerstruktur")
        success = False

    return success


def fix_webportal_permissions(issues):
    """Korrigiert Webportal-Rechte."""
    print("\n→ Korrigiere Webportal-Berechtigungen…\n")

    success = True
    wp_path = "/var/www/html"

    if "wp_missing" in issues:
        print(f"  → Erstelle Webportal-Verzeichnis: {wp_path}")
        result = run_command(f"sudo mkdir -p {wp_path}")
        if result['success']:
            print(f"✓ {wp_path} erstellt")
        else:
            success = False

    if "wp_owner" in issues:
        print(f"  → Setze Besitzer rekursiv: {wp_path} -> {INSTALL_USER}:www-data")
        result = run_command(f"sudo chown -R {INSTALL_USER}:www-data {wp_path}")
        if result['success']:
            print(f"✓ {wp_path}: Besitzer auf {INSTALL_USER}:www-data gesetzt")
        else:
            success = False

    if "wp_mode" in issues:
        print(f"  → Setze Verzeichnis-/Dateirechte rekursiv: {wp_path} -> 775")
        result = run_command(f"sudo chmod -R 775 {wp_path}")
        if result['success']:
            print(f"✓ {wp_path}: Rechte auf 775 gesetzt")
        else:
            success = False

    if "tmp_missing" in issues:
        print(f"  → Erstelle tmp-Verzeichnis: {wp_path}/tmp")
        result = run_command(f"sudo mkdir -p {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Ordner erstellt")
        else:
            success = False

    if "tmp_missing" in issues or "tmp_mode" in issues or "tmp_not_writable" in issues:
        print(f"  → Setze tmp-Rechte rekursiv: {wp_path}/tmp -> 777")
        result = run_command(f"sudo chmod -R 777 {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Rechte korrigiert")
        else:
            success = False

    if "icons_missing" in issues:
        print(f"  → Erstelle Icons-Verzeichnis: {wp_path}/icons")
        result = run_command(f"sudo mkdir -p {wp_path}/icons")
        if result['success']:
            print("✓ icons-Ordner erstellt")
        else:
            success = False

    if "icons_missing" in issues or "icons_mode" in issues:
        print(f"  → Setze Icons-Rechte rekursiv: {wp_path}/icons -> 775")
        result = run_command(f"sudo chmod -R 775 {wp_path}/icons")
        if result['success']:
            print("✓ icons-Rechte korrigiert")
        else:
            success = False

    if "ramdisk_missing" in issues:
        print(f"  → Erstelle RAM-Disk-Verzeichnis: {wp_path}/ramdisk")
        result = run_command(f"sudo mkdir -p {wp_path}/ramdisk")
        if result['success']:
            print("✓ ramdisk-Ordner erstellt")
        else:
            success = False

    if "ramdisk_owner" in issues:
        print(f"  → Setze RAM-Disk Besitzer rekursiv: {wp_path}/ramdisk -> {INSTALL_USER}:www-data")
        result = run_command(f"sudo chown -R {INSTALL_USER}:www-data {wp_path}/ramdisk")
        if result['success']:
            print("✓ ramdisk-Besitzer korrigiert")
        else:
            success = False

    if "ramdisk_missing" in issues or "ramdisk_mode" in issues:
        print(f"  → Setze RAM-Disk-Rechte rekursiv: {wp_path}/ramdisk -> 775 (Dirs), 664 (Dateien)")
        result = run_command(f"sudo chmod -R 775 {wp_path}/ramdisk")
        if result['success']:
            # Fix live.txt spezifisch auf 664
            live_txt = f"{wp_path}/ramdisk/live.txt"
            if os.path.exists(live_txt):
                result2 = run_command(f"sudo chmod 664 {live_txt}")
                if result2['success']:
                    print("✓ ramdisk-Rechte korrigiert (live.txt auf 664)")
                else:
                    print("✓ ramdisk-Rechte korrigiert")
            else:
                print("✓ ramdisk-Rechte korrigiert")
        else:
            success = False

    return success


def cleanup_root_owned_files():
    """Sucht und bereinigt root-eigene Dateien in INSTALL_PATH."""
    if not os.path.exists(INSTALL_PATH):
        return True
    
    print("\n■ Prüfe auf root-eigene Dateien…\n")
    cleaned = 0
    
    try:
        for root, dirs, files in os.walk(INSTALL_PATH):
            # Prüfe Ordner
            for d in dirs:
                dir_path = os.path.join(root, d)
                try:
                    st = os.stat(dir_path)
                    owner = pwd.getpwuid(st.st_uid).pw_name
                    
                    if owner == "root":
                        result = run_command(f"sudo chown {INSTALL_USER}:{INSTALL_USER} {dir_path}")
                        if result['success']:
                            print(f"  ✓ {os.path.relpath(dir_path, INSTALL_PATH)} von root bereinigt")
                            cleaned += 1
                except Exception:
                    pass
            
            # Prüfe Dateien
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    st = os.stat(file_path)
                    owner = pwd.getpwuid(st.st_uid).pw_name
                    
                    if owner == "root":
                        result = run_command(f"sudo chown {INSTALL_USER}:{INSTALL_USER} {file_path}")
                        if result['success']:
                            print(f"  ✓ {os.path.relpath(file_path, INSTALL_PATH)} von root bereinigt")
                            cleaned += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"✗ Fehler beim Scannen: {e}")
        return False
    
    if cleaned > 0:
        print(f"✓ {cleaned} Datei(en)/Ordner bereinigt\n")
    else:
        print("✓ Keine root-eigenen Dateien gefunden\n")
    
    return True


def fix_file_permissions(issues):
    """Korrigiert Datei-Rechte für Konfiguration und ausführbare Dateien."""
    if not issues:
        return True
    
    print("\n→ Korrigiere Datei-Berechtigungen…\n")
    success = True
    
    for path, file_issues in issues.items():
        expected_mode = file_issues.get("expected_mode", "664")
        file_name = os.path.basename(path)

        # Fehlende Dateien anlegen
        if file_issues.get("missing"):
            print(f"  → Erstelle fehlende Datei: {path}")
            result = run_command(f"sudo touch {path}")
            if result['success']:
                print(f"✓ {file_name}: Datei erstellt")
            else:
                success = False
                continue
        
        # Setze Owner auf <user>:www-data
        if file_issues.get("owner") or file_issues.get("group") or file_issues.get("missing"):
            print(f"  → Setze Besitzer: {path} -> {INSTALL_USER}:www-data")
            result = run_command(f"sudo chown {INSTALL_USER}:www-data {path}")
            if result['success']:
                print(f"✓ {file_name}: Besitzer auf {INSTALL_USER}:www-data gesetzt")
            else:
                success = False
        
        # Setze Modus
        if file_issues.get("mode") or file_issues.get("missing"):
            print(f"  → Setze Rechte: {path} -> {expected_mode}")
            result = run_command(f"sudo chmod {expected_mode} {path}")
            if result['success']:
                print(f"✓ {file_name}: Rechte auf {expected_mode} gesetzt")
            else:
                success = False
    
    return success


def run_permissions_wizard():
    """Hauptlogik für Rechteprüfung und -korrektur."""
    # Als erstes: Bereinige root-eigene Dateien falls vorhanden
    cleanup_success = cleanup_root_owned_files()
    if not cleanup_success:
        print("⚠ Warnung: Cleanup von root-Dateien hatte Fehler")
        log_warning("permissions", "Cleanup von root-Dateien hatte Fehler")
    
    issues = check_permissions()
    wp_issues = check_webportal_permissions()
    file_issues = check_file_permissions()

    has_issues = bool(issues) or bool(wp_issues) or bool(file_issues)

    if not has_issues:
        print("\n✓ Alle Berechtigungen sind korrekt.\n")
        perm_logger.info("✓ Permissionsprüfung bestanden: Keine Probleme gefunden.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Berechtigungen korrekt")
        return

    print("\n⚠ Berechtigungsprobleme gefunden.")
    perm_logger.warning(f"⚠ Berechtigungsprobleme erkannt: {len(issues)} Verz., {len(wp_issues)} Web, {len(file_issues)} Dateien")
    choice = input("Automatisch korrigieren? (j/n): ").strip().lower()
    
    if choice != "j":
        print("✗ Korrektur übersprungen.\n")
        perm_logger.warning("✗ Berechtigungskorrektur vom Benutzer übersprungen.")
        log_warning("permissions", "Berechtigungskorrektur vom Benutzer übersprungen")
        return

    all_success = True
    
    if issues:
        success = fix_permissions(issues)
        all_success = all_success and success

    if wp_issues:
        success = fix_webportal_permissions(wp_issues)
        all_success = all_success and success
    
    if file_issues:
        success = fix_file_permissions(file_issues)
        all_success = all_success and success

    if all_success:
        print("\n✓ Alle Berechtigungen korrigiert.\n")
        perm_logger.info("✓ Alle Berechtigungskorektionen erfolgreich durchgeführt.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Probleme behoben")
    else:
        print("\n⚠ Einige Berechtigungen konnten nicht korrigiert werden.\n")
        perm_logger.error("⚠ Einige Berechtigungen konnten nicht automatisch korrigiert werden - manuelle Intervention notwendig.")
        log_error("permissions", "Einige Berechtigungen konnten nicht automatisch korrigiert werden")


register_command("2", "Rechte prüfen & korrigieren", run_permissions_wizard, sort_order=20)
