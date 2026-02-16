import os
import pwd
import grp
import subprocess

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_home_dir, get_www_data_gid

INSTALL_USER = get_install_user()
INSTALL_HOME = get_home_dir(INSTALL_USER)
INSTALL_PATH = get_install_path()


def check_permissions():
    """Prüft Installation-Verzeichnis."""
    print("\n=== Verzeichnis-Rechteprüfung ===\n")

    issues = []

    # Home-Verzeichnis muss für www-data betretbar sein (execute-bit)
    try:
        st_home = os.stat(INSTALL_HOME)
        www_data_gid = get_www_data_gid()
        other_x = bool(st_home.st_mode & 0o001)
        group_x = st_home.st_gid == www_data_gid and bool(st_home.st_mode & 0o010)

        if not other_x and not group_x:
            print(f"✗ {INSTALL_HOME} ist NICHT für www-data erreichbar")
            issues.append("home")
        else:
            print(f"✓ {INSTALL_HOME} ist für www-data erreichbar")
    except Exception as e:
        print(f"✗ Fehler beim Prüfen von {INSTALL_HOME}: {e}")
        issues.append("home")

    # INSTALL_PATH prüfen
    if not os.path.exists(INSTALL_PATH):
        print(f"✓ {INSTALL_PATH} existiert noch nicht – überspringe Rechteprüfung.\n")
        return issues

    if not os.path.isdir(INSTALL_PATH):
        print(f"✗ {INSTALL_PATH} ist kein Verzeichnis!")
        issues.append("notdir")
        return issues

    try:
        st = os.stat(INSTALL_PATH)
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name

        if owner != INSTALL_USER or group != INSTALL_USER:
            print(f"✗ {INSTALL_PATH} gehört {owner}:{group} statt {INSTALL_USER}:{INSTALL_USER}")
            issues.append("owner")
        else:
            print(f"✓ {INSTALL_PATH} gehört {INSTALL_USER}:{INSTALL_USER}")

        mode = oct(st.st_mode)[-3:]
        if mode != "755":
            print(f"✗ {INSTALL_PATH} hat Rechte {mode} statt 755")
            issues.append("mode")
        else:
            print(f"✓ {INSTALL_PATH} hat korrekte Rechte (755)")
    except Exception as e:
        print(f"✗ Fehler beim Prüfen: {e}")
        issues.append("error")

    return issues


def check_webportal_permissions():
    """Prüft Webportal-Verzeichnis."""
    print("\n=== Webportal-Rechteprüfung ===\n")

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

        if owner != INSTALL_USER or group != "www-data":
            print(f"✗ {wp_path} gehört {owner}:{group} statt {INSTALL_USER}:www-data")
            issues.append("wp_owner")
        else:
            print(f"✓ {wp_path} gehört {INSTALL_USER}:www-data")

        mode = oct(st.st_mode)[-3:]
        if mode != "775":
            print(f"✗ {wp_path} hat Rechte {mode} statt 775")
            issues.append("wp_mode")
        else:
            print(f"✓ {wp_path} hat korrekte Rechte (775)")

        # tmp-Ordner prüfen
        tmp_path = f"{wp_path}/tmp"
        if not os.path.exists(tmp_path):
            print(f"✗ {tmp_path} existiert nicht")
            issues.append("tmp_missing")
        else:
            st_tmp = os.stat(tmp_path)
            mode_tmp = oct(st_tmp.st_mode)[-3:]
            if mode_tmp != "777":
                print(f"✗ {tmp_path} hat Rechte {mode_tmp} statt 777")
                issues.append("tmp_mode")
            else:
                print(f"✓ {tmp_path} hat korrekte Rechte (777)")

            www_data_gid = get_www_data_gid()
            other_wx = bool(st_tmp.st_mode & 0o003)
            group_wx = st_tmp.st_gid == www_data_gid and bool(st_tmp.st_mode & 0o030)
            if not other_wx and not group_wx:
                print(f"✗ {tmp_path} ist für www-data nicht schreibbar")
                issues.append("tmp_not_writable")
            else:
                print(f"✓ {tmp_path} ist für www-data schreibbar")

    except Exception as e:
        print(f"✗ Fehler beim Prüfen: {e}")
        issues.append("error")

    return issues


def check_file_permissions():
    """Prüft Dateien, die PHP schreiben muss (config/wallbox) und Python-Dateien."""
    print("\n=== Datei-Rechteprüfung ===\n")

    # Konfigurationsdateien (pi:www-data mit 664)
    config_files = [
        (f"{INSTALL_PATH}/e3dc.config.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.wallbox.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.strompreis.txt", "664")  # Strompreise
    ]
    
    # Ausführbare Python-Dateien (pi:www-data mit 755)
    executable_files = [
        (f"{INSTALL_PATH}/plot_soc_changes.py", "755")
    ]

    # Web-Ausgabedateien (pi:www-data mit 664)
    web_files = [
        ("/var/www/html/diagramm.html", "664"),
        ("/var/www/html/archiv_diagramm.html", "664"),
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
                print(f"✗ {os.path.basename(path)} Problem")
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
                print(f"✗ {os.path.basename(path)} Problem")
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
                print(f"✗ {os.path.basename(path)} Problem")
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
        result = run_command(f"sudo chmod o+x {INSTALL_HOME}")
        if result['success']:
            print(f"✓ {INSTALL_HOME} Rechte korrigiert")
        else:
            success = False

    if "owner" in issues:
        result = run_command(f"sudo chown -R {INSTALL_USER}:{INSTALL_USER} {INSTALL_PATH}")
        if result['success']:
            print("✓ Besitzer korrigiert")
        else:
            success = False

    if "mode" in issues:
        result = run_command(f"sudo chmod -R 755 {INSTALL_PATH}")
        if result['success']:
            print("✓ Rechte korrigiert")
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
        result = run_command(f"sudo mkdir -p {wp_path}")
        if result['success']:
            print(f"✓ {wp_path} erstellt")
        else:
            success = False

    if "wp_owner" in issues:
        result = run_command(f"sudo chown -R {INSTALL_USER}:www-data {wp_path}")
        if result['success']:
            print("✓ Besitzer korrigiert")
        else:
            success = False

    if "wp_mode" in issues:
        result = run_command(f"sudo chmod -R 775 {wp_path}")
        if result['success']:
            print("✓ Rechte korrigiert")
        else:
            success = False

    if "tmp_missing" in issues:
        result = run_command(f"sudo mkdir -p {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Ordner erstellt")
        else:
            success = False

    if "tmp_missing" in issues or "tmp_mode" in issues or "tmp_not_writable" in issues:
        result = run_command(f"sudo chmod -R 777 {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Rechte korrigiert")
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

        # Fehlende Dateien anlegen
        if file_issues.get("missing"):
            result = run_command(f"sudo touch {path}")
            if result['success']:
                print(f"✓ {os.path.basename(path)} erstellt")
            else:
                success = False
                continue
        
        # Setze Owner auf <user>:www-data
        if file_issues.get("owner") or file_issues.get("group") or file_issues.get("missing"):
            result = run_command(f"sudo chown {INSTALL_USER}:www-data {path}")
            if result['success']:
                print(f"✓ {os.path.basename(path)}: Owner korrigiert")
            else:
                success = False
        
        # Setze Modus
        if file_issues.get("mode") or file_issues.get("missing"):
            result = run_command(f"sudo chmod {expected_mode} {path}")
            if result['success']:
                print(f"✓ {os.path.basename(path)}: Rechte auf {expected_mode} gesetzt")
            else:
                success = False
    
    return success


def run_permissions_wizard():
    """Hauptlogik für Rechteprüfung und -korrektur."""
    # Als erstes: Bereinige root-eigene Dateien falls vorhanden
    cleanup_success = cleanup_root_owned_files()
    if not cleanup_success:
        print("⚠ Warnung: Cleanup von root-Dateien hatte Fehler")
    
    issues = check_permissions()
    wp_issues = check_webportal_permissions()
    file_issues = check_file_permissions()

    has_issues = bool(issues) or bool(wp_issues) or bool(file_issues)

    if not has_issues:
        print("\n✓ Alle Berechtigungen sind korrekt.\n")
        return

    print("\n⚠ Berechtigungsprobleme gefunden.")
    choice = input("Automatisch korrigieren? (j/n): ").strip().lower()
    
    if choice != "j":
        print("✗ Korrektur übersprungen.\n")
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
    else:
        print("\n⚠ Einige Berechtigungen konnten nicht korrigiert werden.\n")


register_command("1", "Rechte prüfen & korrigieren", run_permissions_wizard, sort_order=10)
