import os
import pwd
import grp
import subprocess
import logging

from .core import register_command
from .utils import run_command, setup_logging
from .installer_config import CONFIG_FILE, get_install_path, get_install_user, get_home_dir, get_www_data_gid
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_USER = get_install_user()
INSTALL_HOME = get_home_dir(INSTALL_USER)
INSTALL_PATH = get_install_path()


def _strip_utf8_bom(path):
    """Entfernt UTF-8 BOM, falls vorhanden."""
    try:
        with open(path, "rb") as f:
            content = f.read()
        bom = b"\xef\xbb\xbf"
        if content.startswith(bom):
            with open(path, "wb") as f:
                f.write(content[len(bom):])
            print(f"  ✓ BOM entfernt: {os.path.basename(path)}")
    except Exception:
        pass


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
            (f"{wp_path}/ramdisk", "775"),
            (f"{wp_path}/tmp/history_backups", "775")
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


# Definition der zu prüfenden Dateien und ihrer Berechtigungen
FILE_DEFINITIONS = [
    # Installer Config (Sonderfall, weil sie nicht im INSTALL_PATH liegen muss)
    {"path": CONFIG_FILE, "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Konfigurationsdateien
    {"path": f"{INSTALL_PATH}/e3dc.config.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALL_PATH}/e3dc.wallbox.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": f"{INSTALL_PATH}/e3dc.strompreis.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Ausführbare Python-Dateien
    {"path": f"{INSTALL_PATH}/plot_soc_changes.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALL_PATH}/plot_live_history.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALL_HOME}/get_live.sh", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    # Web-Ausgabedateien
    {"path": "/var/www/html/index.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/helpers.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/Wallbox.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/mobile.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/archiv.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/get_live_json.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/run_live_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/run_now.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/run_update.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/config_editor.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/archiv_diagramm.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/run_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/backup_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/archiv_diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/diagramm_mobile.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/live_diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/e3dc_paths.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/sw.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/manifest.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/status.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/ramdisk/live.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/ramdisk/live_history.txt", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_done", "mode": "666", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_done_archiv", "mode": "666", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_done_mobile", "mode": "666", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_error", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_error_archiv", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_error_mobile", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/tmp/plot_live_history_last_run", "mode": "666", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/tmp/plot_soc_last_run", "mode": "666", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
]


def check_file_permissions():
    """Prüft Dateien, die PHP schreiben muss (config/wallbox) und Python-Dateien."""
    print("\n=== Datei-Rechteprüfung ===\n")
    issues = {}
    for fdef in FILE_DEFINITIONS:
        path = fdef["path"]
        expected_mode = fdef["mode"]
        expected_owner = fdef["owner"]
        expected_group = fdef["group"]
        is_optional = fdef["optional"]
        is_executable = fdef["executable"]
        file_name = os.path.basename(path)

        if not os.path.exists(path):
            if not is_optional:
                print(f"✗ {file_name} fehlt")
                issues[path] = {"missing": True}
            continue
        try:
            st = os.stat(path)
            mode = oct(st.st_mode)[-3:]
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            owner_ok = owner == expected_owner
            group_ok = group == expected_group
            mode_ok = mode == expected_mode
            exec_ok = not is_executable or (is_executable and bool(st.st_mode & 0o111))
            if owner_ok and group_ok and mode_ok and exec_ok:
                exec_str = ", ausführbar" if is_executable else ""
                print(f"✓ {file_name} OK ({expected_owner}:{expected_group}, {expected_mode}{exec_str})")
            else:
                details = []
                if not owner_ok: details.append(f"Owner={owner} (soll: {expected_owner})")
                if not group_ok: details.append(f"Gruppe={group} (soll: {expected_group})")
                if not mode_ok: details.append(f"Modus={mode} (soll: {expected_mode})")
                if not exec_ok: details.append("nicht ausführbar")
                print(f"✗ {file_name} Problem: {', '.join(details)}")
                issues[path] = {
                    "owner": not owner_ok,
                    "group": not group_ok,
                    "mode": not mode_ok,
                    "exec": not exec_ok
                }
        except Exception as e:
            print(f"✗ Fehler bei {path}: {e}")
            issues[path] = {"error": str(e)}
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
            # Fix live.txt und live_history.txt spezifisch auf 664
            for fname in ("live.txt", "live_history.txt"):
                live_f = f"{wp_path}/ramdisk/{fname}"
                if os.path.exists(live_f):
                    run_command(f"sudo chmod 664 {live_f}")
            print("✓ ramdisk-Rechte korrigiert")
        else:
            success = False
    # Hinzugefügt für History-Backups
    if "history_backups_missing" in issues:
        print(f"  → Erstelle Backup-Verzeichnis: {wp_path}/tmp/history_backups")
        result = run_command(f"sudo mkdir -p {wp_path}/tmp/history_backups")
        if result['success']:
            print("✓ history_backups-Ordner erstellt")
        else:
            success = False
    if "history_backups_owner" in issues:
        print(f"  → Setze Backup-Verzeichnis Besitzer: {wp_path}/tmp/history_backups -> {INSTALL_USER}:www-data")
        result = run_command(f"sudo chown -R {INSTALL_USER}:www-data {wp_path}/tmp/history_backups")
        if result['success']:
            print("✓ history_backups-Besitzer korrigiert")
        else:
            success = False
    if "history_backups_missing" in issues or "history_backups_mode" in issues:
        print(f"  → Setze Backup-Verzeichnis Rechte: {wp_path}/tmp/history_backups -> 775")
        result = run_command(f"sudo chmod -R 775 {wp_path}/tmp/history_backups")
        if result['success']:
            print("✓ history_backups-Rechte korrigiert")
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
    """Korrigiert Datei-Rechte basierend auf den FILE_DEFINITIONS."""
    if not issues:
        return True
    print("\n→ Korrigiere Datei-Berechtigungen…\n")
    success = True
    # Erstelle eine Map von Pfad zu Definition für schnellen Zugriff
    defs_map = {d["path"]: d for d in FILE_DEFINITIONS}
    for path, file_issues in issues.items():
        if path not in defs_map:
            perm_logger.warning(f"Keine Definition für Pfad gefunden: {path}")
            continue
        definition = defs_map[path]
        expected_owner = definition["owner"]
        expected_group = definition["group"]
        expected_mode = definition["mode"]
        file_name = os.path.basename(path)
        # Fehlende Dateien anlegen
        if file_issues.get("missing"):
            print(f"  → Erstelle fehlende Datei: {path}")
            # `touch` erstellt eine leere Datei, die danach weiter bearbeitet wird
            result = run_command(f"sudo touch {path}")
            if result['success']:
                print(f"✓ {file_name}: Datei erstellt")
            else:
                success = False
                perm_logger.error(f"Konnte fehlende Datei nicht erstellen: {path}")
                continue  # Springe zur nächsten Datei wenn Erstellen fehlschlägt
        # Setze Besitzer:Gruppe, wenn Problem vorliegt oder Datei neu erstellt wurde
        if file_issues.get("owner") or file_issues.get("group") or file_issues.get("missing"):
            print(f"  → Setze Besitzer: {path} -> {expected_owner}:{expected_group}")
            result = run_command(f"sudo chown {expected_owner}:{expected_group} {path}")
            if result['success']:
                print(f"✓ {file_name}: Besitzer auf {expected_owner}:{expected_group} gesetzt")
            else:
                success = False
                perm_logger.error(f"Besitzer für {file_name} konnte nicht gesetzt werden.")
        # Setze Modus, wenn Problem vorliegt oder Datei neu erstellt wurde
        if file_issues.get("mode") or file_issues.get("missing") or file_issues.get("exec"):
            print(f"  → Setze Rechte: {path} -> {expected_mode}")
            result = run_command(f"sudo chmod {expected_mode} {path}")
            if result['success']:
                print(f"✓ {file_name}: Rechte auf {expected_mode} gesetzt")
            else:
                success = False
                perm_logger.error(f"Rechte für {file_name} konnten nicht auf {expected_mode} gesetzt werden.")
        # BOM still entfernen bei Skripten (Shebang-Kompatibilitaet)
        if file_name.endswith((".py", ".sh")):
            _strip_utf8_bom_silent(path)
    return success

def check_cronjobs():
    """Prüft, ob die notwendigen Cronjobs für den INSTALL_USER existieren."""
    print("\n=== Cronjob-Prüfung ===\n")
    perm_logger.info("--- Starte Cronjob-Prüfung ---")
    issues = {}

    expected_cronjobs = [
        {
            "name": "Live-History schreiben",
            "line": "* * * * * cd /var/www/html && /usr/bin/php get_live_json.php > /dev/null 2>&1",
            "check_part": "get_live_json.php",
            "optional_if_exists": "/var/www/html/get_live_json.php"
        },
        {
            "name": "History-Backup",
            "line": "0 0 * * * /usr/bin/php /var/www/html/backup_history.php > /dev/null 2>&1",
            "check_part": "/var/www/html/backup_history.php",
            "optional_if_exists": "/var/www/html/backup_history.php"
        },
        {
            "name": "E3DC-Control Autostart",
            "line": f"@reboot sleep 20 && echo 0 > {INSTALL_PATH}/stop && /usr/bin/screen -dmS E3DC {INSTALL_PATH}/E3DC.sh",
            "check_part": "screen -dmS E3DC",
            "optional_if_exists": f"{INSTALL_PATH}/E3DC.sh"
        },
        {
            "name": "Live-Grabber Autostart",
            "line": f"@reboot /usr/bin/screen -dmS live-grabber {INSTALL_HOME}/get_live.sh",
            "check_part": "screen -dmS live-grabber",
            "optional_if_exists": f"{INSTALL_HOME}/get_live.sh"
        },
        {
            "name": "Boot-Benachrichtigung",
            "line": "@reboot sleep 45 && /usr/local/bin/boot_notify.sh",
            "check_part": "boot_notify.sh",
            "optional_if_exists": "/usr/local/bin/boot_notify.sh"
        },
        {
            "name": "Täglicher Statusbericht",
            "line": '0 12 * * * /usr/local/bin/boot_notify.sh "✅ Status:ControlReserve Online. Laufzeit: \\$(uptime -p)"',
            "check_part": 'boot_notify.sh "✅ Status:',
            "optional_if_exists": "/usr/local/bin/boot_notify.sh"
        }
    ]

    try:
        result = run_command(f"sudo crontab -l -u {INSTALL_USER}")
        current_crontab = result['stdout'] if result['success'] else ""

        for cron in expected_cronjobs:
            # NEU: Prüfen, ob die zugehörige Datei existiert, bevor der Cronjob geprüft wird
            if 'optional_if_exists' in cron and not os.path.exists(cron['optional_if_exists']):
                perm_logger.info(f"Cronjob '{cron['name']}' übersprungen (optionales Modul {cron['optional_if_exists']} nicht gefunden).")
                continue

            if cron['check_part'] in current_crontab:
                print(f"✓ Cronjob '{cron['name']}' gefunden.")
                perm_logger.info(f"Cronjob '{cron['name']}' gefunden.")
            else:
                print(f"✗ Cronjob '{cron['name']}' fehlt.")
                perm_logger.warning(f"Cronjob '{cron['name']}' fehlt.")
                issues[cron['name']] = {"missing": True, "line": cron['line']}

    except Exception as e:
        print(f"✗ Fehler beim Prüfen der Cronjobs: {e}")
        perm_logger.error(f"Fehler beim Prüfen der Cronjobs: {e}")
        issues["error"] = str(e)

    return issues

def fix_cronjobs(issues):
    """Fügt fehlende Cronjob-Einträge hinzu."""
    print("\n→ Korrigiere Cronjob-Einträge…\n")
    success = True

    for name, issue_details in issues.items():
        if issue_details.get("missing"):
            cron_line = issue_details["line"]
            print(f"  → Füge Cronjob '{name}' hinzu...")

            # Anführungszeichen für den Shell-Befehl escapen, damit echo "..." funktioniert
            cron_line_safe = cron_line.replace('"', '\\"')

            # Dieser Befehl fügt die Zeile zur Crontab hinzu, falls sie noch nicht existiert.
            # Er ist sicher gegen Duplikate.
            cmd = f"sudo bash -c \"(crontab -u {INSTALL_USER} -l 2>/dev/null | grep -Fq -- \\\"{cron_line_safe}\\\") || (crontab -u {INSTALL_USER} -l 2>/dev/null; echo \\\"{cron_line_safe}\\\") | crontab -u {INSTALL_USER} -\""
            
            result = run_command(cmd)
            
            if result['success']:
                print(f"✓ Cronjob '{name}' hinzugefügt/verifiziert.")
                perm_logger.info(f"Cronjob '{name}' hinzugefügt/verifiziert: {cron_line}")
            else:
                print(f"✗ Fehler beim Hinzufügen des Cronjobs '{name}'. Details im Log.")
                perm_logger.error(f"Fehler beim Hinzufügen des Cronjobs '{name}': {result['stderr']}")
                success = False
                
    return success


def check_sudoers_permissions():
    """Prüft, ob www-data das Update-Skript ausführen darf."""
    print("\n=== Sudoers-Prüfung (Web-Update) ===\n")
    perm_logger.info("--- Starte Sudoers-Prüfung ---")
    
    sudoers_file = "/etc/sudoers.d/010_e3dc_web_update"
    script_path = os.path.join(INSTALL_HOME, "Install", "installer_main.py")
    expected_content = f"www-data ALL=(root) NOPASSWD: /usr/bin/python3 {script_path} --update-e3dc"
    
    issues = []
    
    if not os.path.exists(sudoers_file):
        print(f"✗ Sudoers-Datei fehlt: {sudoers_file}")
        issues.append({"missing": True, "file": sudoers_file, "content": expected_content})
    else:
        try:
            with open(sudoers_file, "r") as f:
                content = f.read().strip()
            if content != expected_content:
                print(f"✗ Sudoers-Inhalt veraltet/falsch.")
                issues.append({"missing": False, "file": sudoers_file, "content": expected_content})
            else:
                print(f"✓ Sudoers-Konfiguration korrekt.")
                perm_logger.info("Sudoers-Konfiguration korrekt.")
        except Exception as e:
            print(f"✗ Fehler beim Lesen von {sudoers_file}: {e}")
            perm_logger.error(f"Fehler beim Lesen von {sudoers_file}: {e}")
            issues.append({"error": True})
            
    return issues

def fix_sudoers_permissions(issues):
    """Erstellt die Sudoers-Datei für Web-Updates."""
    print("\n→ Richte Sudoers für Web-Update ein…\n")
    success = True
    for issue in issues:
        if "content" in issue:
            path = issue["file"]
            content = issue["content"]
            print(f"  → Schreibe {path}…")
            try:
                run_command(f"sudo bash -c 'echo \"{content}\" > {path}'")
                run_command(f"sudo chmod 440 {path}")
                print(f"✓ Sudoers-Datei erstellt/aktualisiert.")
                perm_logger.info(f"Sudoers-Datei erstellt/aktualisiert: {path}")
            except Exception as e:
                print(f"✗ Fehler: {e}")
                perm_logger.error(f"Fehler beim Erstellen der Sudoers-Datei {path}: {e}")
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
    cron_issues = check_cronjobs()
    sudo_issues = check_sudoers_permissions()

    has_issues = bool(issues) or bool(wp_issues) or bool(file_issues) or bool(cron_issues) or bool(sudo_issues)
    if not has_issues:
        print("\n✓ Alle Berechtigungen und Cronjobs sind korrekt.\n")
        perm_logger.info("✓ Prüfung bestanden: Keine Probleme bei Berechtigungen oder Cronjobs gefunden.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Berechtigungen und Cronjobs korrekt")
        return

    print("\n⚠ Probleme mit Berechtigungen und/oder Cronjobs gefunden.")
    perm_logger.warning(f"⚠ Probleme erkannt: {len(issues)} Verz., {len(wp_issues)} Web, {len(file_issues)} Dateien, {len(cron_issues)} Cronjobs, {len(sudo_issues)} Sudoers")
    choice = input("Automatisch korrigieren? (j/n): ").strip().lower()
    if choice != "j":
        print("✗ Korrektur übersprungen.\n")
        perm_logger.warning("✗ Korrektur vom Benutzer übersprungen.")
        log_warning("permissions", "Korrektur vom Benutzer übersprungen")
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
    if cron_issues:
        success = fix_cronjobs(cron_issues)
        all_success = all_success and success
    if sudo_issues:
        success = fix_sudoers_permissions(sudo_issues)
        all_success = all_success and success

    if all_success:
        print("\n✓ Alle Probleme korrigiert.\n")
        perm_logger.info("✓ Alle Korrekturen erfolgreich durchgeführt.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Probleme behoben")
    else:
        print("\n⚠ Einige Probleme konnten nicht korrigiert werden.\n")
        perm_logger.error("⚠ Einige Probleme konnten nicht automatisch korrigiert werden - manuelle Intervention notwendig.")
        log_error("permissions", "Einige Probleme konnten nicht automatisch korrigiert werden")


register_command("2", "Rechte prüfen & korrigieren", run_permissions_wizard, sort_order=20)
