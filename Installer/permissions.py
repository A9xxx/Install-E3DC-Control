import os
import pwd
import grp
import subprocess

from .core import register_command
from .utils import run_command

INSTALL_PATH = "/home/pi/E3DC-Control"


def check_permissions():
    """Prüft Installation-Verzeichnis."""
    print("\n=== Verzeichnis-Rechteprüfung ===\n")

    issues = []

    # /home/pi muss für Webserver lesbar sein
    if not os.access("/home/pi", os.X_OK):
        print("✗ /home/pi ist NICHT für Webserver lesbar")
        issues.append("home")
    else:
        print("✓ /home/pi ist für Webserver lesbar")

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

        if owner != "pi" or group != "pi":
            print(f"✗ {INSTALL_PATH} gehört {owner}:{group} statt pi:pi")
            issues.append("owner")
        else:
            print(f"✓ {INSTALL_PATH} gehört pi:pi")

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

        if owner != "pi" or group != "www-data":
            print(f"✗ {wp_path} gehört {owner}:{group} statt pi:www-data")
            issues.append("wp_owner")
        else:
            print(f"✓ {wp_path} gehört pi:www-data")

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

    except Exception as e:
        print(f"✗ Fehler beim Prüfen: {e}")
        issues.append("error")

    return issues


def check_file_permissions():
    """Prüft Python-Dateien, die PHP schreiben muss."""
    print("\n=== Datei-Rechteprüfung ===\n")

    files_to_check = [
        f"{INSTALL_PATH}/plot_soc_changes.py",
        f"{INSTALL_PATH}/e3dc.config.txt",
        f"{INSTALL_PATH}/e3dc.wallbox.txt",
        f"{INSTALL_PATH}/e3dc.strompreis.txt"
    ]
    
    issues = {}
    
    for path in files_to_check:
        if not os.path.exists(path):
            continue
        
        try:
            st = os.stat(path)
            mode = oct(st.st_mode)[-3:]
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            
            is_group_writable = bool(st.st_mode & 0o020)
            status_ok = owner == "pi" and group == "pi" and mode == "664" and is_group_writable
            
            if status_ok:
                print(f"✓ {path} OK")
            else:
                print(f"✗ {path} Problem")
                issues[path] = {
                    "owner": owner != "pi",
                    "group": group != "pi",
                    "mode": mode != "664"
                }
        except Exception as e:
            print(f"✗ Fehler bei {path}: {e}")
    
    return issues


def fix_permissions(issues):
    """Korrigiert Installation-Verzeichnis-Rechte."""
    print("\n→ Korrigiere Verzeichnis-Berechtigungen…\n")

    success = True

    if "home" in issues:
        result = run_command("sudo chmod o+rx /home/pi")
        if result['success']:
            print("✓ /home/pi Rechte korrigiert")
        else:
            success = False

    if "owner" in issues:
        result = run_command(f"sudo chown -R pi:pi {INSTALL_PATH}")
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
        result = run_command(f"sudo chown -R pi:www-data {wp_path}")
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

    if "tmp_missing" in issues or "tmp_mode" in issues:
        result = run_command(f"sudo chmod -R 777 {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Rechte korrigiert")
        else:
            success = False

    return success


def fix_file_permissions(issues):
    """Korrigiert Datei-Rechte für PHP-Zugriff."""
    if not issues:
        return True
    
    print("\n→ Korrigiere Datei-Berechtigungen…\n")
    success = True
    
    for path, file_issues in issues.items():
        if file_issues["owner"] or file_issues["group"]:
            result = run_command(f"sudo chown pi:pi {path}")
            if not result['success']:
                success = False
        
        if file_issues["mode"]:
            result = run_command(f"sudo chmod 664 {path}")
            if not result['success']:
                success = False
    
    return success


def run_permissions_wizard():
    """Hauptlogik für Rechteprüfung und -korrektur."""
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
