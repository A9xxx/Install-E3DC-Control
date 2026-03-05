import os
import pwd
import grp
import subprocess
import logging
import tempfile

from .core import register_command
from .utils import run_command
from .installer_config import CONFIG_FILE, get_install_path, get_install_user, get_home_dir, get_www_data_gid, load_config
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
            
        # VENV prüfen (falls vorhanden)
        venv_name = load_config().get("venv_name", ".venv_e3dc")
        venv_path = ""
        if venv_name:
            if os.path.exists(os.path.join(INSTALL_HOME, venv_name)):
                venv_path = os.path.join(INSTALL_HOME, venv_name)
            elif os.path.exists(os.path.join(INSTALL_PATH, venv_name)):
                venv_path = os.path.join(INSTALL_PATH, venv_name)

        if venv_name and venv_path:
            st_venv = os.stat(venv_path)
            owner_venv = pwd.getpwuid(st_venv.st_uid).pw_name
            if owner_venv != INSTALL_USER:
                print(f"✗ {venv_name} gehört {owner_venv} (soll: {INSTALL_USER})")
                issues.append("venv_owner")
            else:
                print(f"✓ {venv_name} gehört {INSTALL_USER}")
            
            # Prüfen ob executables ausführbar sind
            pip_bin = os.path.join(venv_path, "bin", "pip")
            if os.path.exists(pip_bin) and not os.access(pip_bin, os.X_OK):
                print(f"✗ {venv_name}/bin/pip ist nicht ausführbar")
                issues.append("venv_mode")

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
            (f"{wp_path}/ramdisk", "2775"),
            (f"{wp_path}/tmp/history_backups", "775")
        ]
        for folder_path, expected_mode in subfolders:
            if not os.path.exists(folder_path):
                print(f"✗ {folder_path} existiert nicht")
                issues.append(f"{os.path.basename(folder_path)}_missing")
            else:
                st_sub = os.stat(folder_path)
                # Mode-Erkennung: 4 Stellen für S-Bit (z.B. 2775), sonst 3
                if len(expected_mode) == 4:
                    mode_sub = oct(st_sub.st_mode)[-4:]
                else:
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
    {"path": f"{INSTALL_PATH}/diagram_config.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    # Ausführbare Python-Dateien
    {"path": f"{INSTALL_PATH}/plot_soc_changes.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALL_PATH}/plot_live_history.py", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    {"path": f"{INSTALL_HOME}/get_live.sh", "mode": "755", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": True},
    # Web-Ausgabedateien
    {"path": "/var/www/html/index.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/helpers.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/logic.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/solar.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/Wallbox.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/mobile.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/archiv.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/get_live_json.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/config_editor.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/backup_history.php", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False},
    {"path": "/var/www/html/diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/archiv_diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/diagramm_mobile.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/live_diagramm.html", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/e3dc_paths.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/sw.js", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
    {"path": "/var/www/html/manifest.json", "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": False, "executable": False},
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
    
    # Logfile aus Config lesen und zur globalen Liste hinzufügen
    try:
        config_path = os.path.join(INSTALL_PATH, "e3dc.config.txt")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().lower().startswith("logfile"):
                        parts = line.split("=")
                        if len(parts) > 1:
                            log_val = parts[1].strip().strip('"').strip("'")
                            if log_val:
                                full_log_path = log_val if log_val.startswith("/") else os.path.join(INSTALL_PATH, log_val)
                                # Prüfen ob schon vorhanden
                                if not any(d['path'] == full_log_path for d in FILE_DEFINITIONS):
                                    FILE_DEFINITIONS.append({
                                        "path": full_log_path, "mode": "664", "owner": INSTALL_USER, "group": "www-data", "optional": True, "executable": False
                                    })
                                    print(f"ℹ️  Logdatei erkannt: {os.path.basename(full_log_path)}")
                                break
    except Exception:
        pass

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
    if "venv_owner" in issues:
        venv_name = load_config().get("venv_name", ".venv_e3dc")
        print(f"  → Setze Besitzer für {venv_name}: {INSTALL_USER}:{INSTALL_USER}")
        # Pfad erneut ermitteln
        venv_path = os.path.join(INSTALL_HOME, venv_name)
        if not os.path.exists(venv_path) and os.path.exists(os.path.join(INSTALL_PATH, venv_name)):
            venv_path = os.path.join(INSTALL_PATH, venv_name)
            
        result = run_command(f"sudo chown -R {INSTALL_USER}:{INSTALL_USER} {venv_path}")
        if result['success']:
            print(f"✓ {venv_name} Besitzer korrigiert")
        else:
            success = False
    if "venv_mode" in issues:
        venv_name = load_config().get("venv_name", ".venv_e3dc")
        print(f"  → Setze Rechte für {venv_name}/bin: +x")
        # Pfad erneut ermitteln
        venv_bin = os.path.join(INSTALL_HOME, venv_name, "bin")
        if not os.path.exists(venv_bin) and os.path.exists(os.path.join(INSTALL_PATH, venv_name, "bin")):
            venv_bin = os.path.join(INSTALL_PATH, venv_name, "bin")
            
        result = run_command(f"sudo chmod -R +x {venv_bin}")
        if result['success']:
            print(f"✓ {venv_name} Executables korrigiert")
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
    if "tmp_owner" in issues:
        print(f"  → Setze tmp-Besitzer rekursiv: {wp_path}/tmp -> {INSTALL_USER}:www-data")
        result = run_command(f"sudo chown -R {INSTALL_USER}:www-data {wp_path}/tmp")
        if result['success']:
            print("✓ tmp-Besitzer korrigiert")
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
        print(f"  → Setze RAM-Disk-Rechte: {wp_path}/ramdisk -> 2775")
        result = run_command(f"sudo chmod 2775 {wp_path}/ramdisk")
        if result['success']:
            # Fix live.txt und live_history.txt spezifisch auf 664
            for fname in ("live.txt", "live_history.txt", "luxtronik.json", "luxtronik_history.json", "luxtronik_stats.json"):
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
            _strip_utf8_bom(path)
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
            "line": "0 0 * * * /usr/bin/php /var/www/html/backup_history.php > /dev/null 2>&1 # E3DC-Control History Backup",
            "check_part": "/var/www/html/backup_history.php",
            "optional_if_exists": "/var/www/html/backup_history.php"
        },
        {
            "name": "Boot-Benachrichtigung",
            "line": "@reboot sleep 45 && /usr/local/bin/boot_notify.sh",
            "check_part": "sleep 45 && /usr/local/bin/boot_notify.sh",
            "optional_if_exists": "/usr/local/bin/boot_notify.sh"
        },
        {
            "name": "Täglicher Statusbericht",
            "line": "0 12 * * * /usr/local/bin/boot_notify.sh status",
            "check_part": "/usr/local/bin/boot_notify.sh status",
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
    
    # 1. Aktuelle Crontab lesen
    result = run_command(f"sudo crontab -u {INSTALL_USER} -l")
    current_crontab = result['stdout'] if result['success'] else ""
    
    # Zeilenweise verarbeiten und leere Zeilen entfernen
    new_crontab_lines = [line for line in current_crontab.splitlines() if line.strip()]
    modified = False

    for name, issue_details in issues.items():
        if issue_details.get("missing"):
            cron_line = issue_details["line"]
            print(f"  → Füge Cronjob '{name}' hinzu...")
            
            # Prüfen ob exakte Zeile schon existiert (vermeidet Duplikate)
            if cron_line in new_crontab_lines:
                print("    (Bereits vorhanden, überspringe)")
                continue
            
            new_crontab_lines.append(cron_line)
            modified = True
            perm_logger.info(f"Cronjob '{name}' zur Liste hinzugefügt.")

    if modified:
        try:
            # Temporäre Datei erstellen (vermeidet Shell-Escaping-Probleme mit Sonderzeichen)
            # WICHTIG: encoding='utf-8' für Emojis
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp:
                # Inhalt schreiben (mit abschließendem Newline)
                tmp.write("\n".join(new_crontab_lines) + "\n")
                tmp_path = tmp.name
            
            # Crontab aus Datei laden
            cmd = f"sudo crontab -u {INSTALL_USER} {tmp_path}"
            result = run_command(cmd)
            
            # Temp-Datei aufräumen
            os.unlink(tmp_path)
            
            if result['success']:
                print(f"✓ Crontab erfolgreich aktualisiert.")
                perm_logger.info("Crontab erfolgreich geschrieben.")
            else:
                print(f"✗ Fehler beim Schreiben der Crontab: {result['stderr']}")
                perm_logger.error(f"Fehler beim Schreiben der Crontab: {result['stderr']}")
                success = False
        except Exception as e:
            print(f"✗ Fehler: {e}")
            perm_logger.error(f"Exception beim Schreiben der Crontab: {e}")
            success = False
    else:
        print("✓ Keine Änderungen notwendig.")
                
    return success


def check_sudoers_permissions():
    """Prüft, ob www-data die notwendigen Sudo-Rechte für Web-Funktionen hat."""
    print("\n=== Sudoers-Prüfung (Web-Funktionen) ===\n")
    perm_logger.info("--- Starte Sudoers-Prüfung ---")

    # Dynamischer Pfad zum Installer-Skript (basierend auf aktuellem Speicherort)
    current_installer_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    script_path = os.path.join(current_installer_dir, "installer_main.py")

    expected_sudoers_files = [
        {
            "file": "/etc/sudoers.d/010_e3dc_web_update",
            "content": f"www-data ALL=(root) NOPASSWD: /usr/bin/python3 {script_path} --update-e3dc",
            "description": "Web-Update"
        },
        {
            "file": "/etc/sudoers.d/010_e3dc_web_git",
            "content": "www-data ALL=(root) NOPASSWD: /usr/bin/git, /bin/systemctl",
            "description": "Web-Steuerung (git/systemctl)"
        }
    ]

    issues = []

    for sudo_def in expected_sudoers_files:
        sudoers_file = sudo_def["file"]
        expected_content = sudo_def["content"]
        description = sudo_def["description"]

        if not os.path.exists(sudoers_file):
            print(f"✗ Sudoers-Datei für '{description}' fehlt: {os.path.basename(sudoers_file)}")
            issues.append({"missing": True, "file": sudoers_file, "content": expected_content})
        else:
            try:
                with open(sudoers_file, "r") as f:
                    content = f.read().strip()
                if content != expected_content:
                    print(f"✗ Sudoers-Inhalt für '{description}' veraltet/falsch.")
                    issues.append({"missing": False, "file": sudoers_file, "content": expected_content})
                else:
                    print(f"✓ Sudoers-Konfiguration für '{description}' korrekt.")
                    perm_logger.info(f"Sudoers-Konfiguration '{description}' korrekt.")
            except Exception as e:
                print(f"✗ Fehler beim Lesen von {sudoers_file}: {e}")
                perm_logger.error(f"Fehler beim Lesen von {sudoers_file}: {e}")
                issues.append({"error": True, "file": sudoers_file})
    return issues

def fix_sudoers_permissions(issues):
    """Erstellt oder korrigiert die Sudoers-Dateien für Web-Funktionen."""
    print("\n→ Richte Sudoers für Web-Funktionen ein…\n")
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

def check_services():
    """Prüft, ob Services laufen (E3DC-Control, Watchdog)."""
    print("\n=== Service-Prüfung ===\n")
    perm_logger.info("--- Starte Service-Prüfung ---")
    issues = {}

    services_to_check = []

    # E3DC-Control Service
    if os.path.exists("/etc/systemd/system/e3dc.service"):
        services_to_check.append("e3dc")

    # Piguard (Watchdog) - Nur prüfen, wenn das Skript existiert (also installiert wurde)
    if os.path.exists("/usr/local/bin/pi_guard.sh"):
        services_to_check.append("piguard")

    # Live-Grabber Service
    if os.path.exists("/etc/systemd/system/e3dc-grabber.service"):
        services_to_check.append("e3dc-grabber")

    for srv in services_to_check:
        # Check status
        res_active = run_command(f"systemctl is-active {srv}")
        is_active = res_active['stdout'].strip() == "active"
        
        res_enabled = run_command(f"systemctl is-enabled {srv}")
        is_enabled = res_enabled['stdout'].strip() == "enabled"
        
        if is_active and is_enabled:
            print(f"✓ Service '{srv}' ist aktiv und enabled.")
            perm_logger.info(f"Service '{srv}' OK.")
        else:
            details = []
            if not is_active: details.append("nicht aktiv")
            if not is_enabled: details.append("nicht enabled")
            print(f"✗ Service '{srv}' Problem: {', '.join(details)}")
            perm_logger.warning(f"Service '{srv}' Problem: {', '.join(details)}")
            issues[srv] = {"active": is_active, "enabled": is_enabled}
    
    return issues

def fix_services(issues):
    """Korrigiert Service-Status."""
    print("\n→ Korrigiere Services…\n")
    success = True
    for srv, data in issues.items():
        if not data.get("enabled"):
            print(f"  → Enable {srv}...")
            run_command(f"sudo systemctl enable {srv}")
        
        if not data.get("active"):
            print(f"  → Start {srv}...")
            run_command(f"sudo systemctl start {srv}")
            
        # Verify
        res = run_command(f"systemctl is-active {srv}")
        if res['stdout'].strip() == "active":
            print(f"✓ {srv} läuft nun.")
        else:
            print(f"✗ {srv} konnte nicht gestartet werden.")
            success = False
    return success

def check_legacy_autostart():
    """Prüft auf alte Autostart-Einträge in /etc/rc.local."""
    print("\n=== Legacy Autostart Prüfung ===\n")
    perm_logger.info("--- Starte Legacy Autostart Prüfung ---")
    issues = []
    rc_local = "/etc/rc.local"
    
    if os.path.exists(rc_local):
        try:
            with open(rc_local, "r") as f:
                for line in f:
                    if "E3DC.sh" in line and "screen" in line and not line.strip().startswith("#"):
                        print(f"✗ Alter Autostart in {rc_local} gefunden: {line.strip()}")
                        issues.append("rc_local_legacy")
                        break
        except Exception as e:
            print(f"⚠ Fehler beim Lesen von {rc_local}: {e}")
    
    if not issues:
        print("✓ Keine Legacy-Einträge in rc.local gefunden.")
    
    return issues

def fix_legacy_autostart(issues):
    """Entfernt Legacy-Einträge und bereinigt Prozesse."""
    print("\n→ Bereinige Legacy Autostart…\n")
    success = True
    
    if "rc_local_legacy" in issues:
        rc_local = "/etc/rc.local"
        try:
            with open(rc_local, "r") as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if "E3DC.sh" in line and "screen" in line and not line.strip().startswith("#"):
                    continue
                new_lines.append(line)
            with open(rc_local, "w") as f:
                f.writelines(new_lines)
            print(f"✓ {rc_local} bereinigt.")
            
            # Laufende Screen-Sessions killen (sowohl User als auch Root/Andere)
            print("  → Beende laufende E3DC Screen-Sessions (Cleanup)...")
            run_command(f"sudo -u {INSTALL_USER} screen -S E3DC -X quit")
            run_command("sudo screen -S E3DC -X quit")
            
            # Service neu starten, um sauberen Zustand zu haben
            print("  → Starte E3DC-Service neu...")
            run_command("sudo systemctl restart e3dc")
            print("✓ Service neu gestartet.")
            perm_logger.info("Legacy Autostart entfernt und Service neu gestartet.")
            
        except Exception as e:
            print(f"✗ Fehler: {e}")
            perm_logger.error(f"Fehler beim Fixen von Legacy Autostart: {e}")
            success = False
            
    return success

    return success


def check_and_set_config_defaults():
    """Prüft, ob wichtige UI-bezogene Variablen in e3dc.config.txt vorhanden sind und fügt sie bei Bedarf hinzu."""
    print("\n=== Konfigurations-Standardwerte-Prüfung ===\n")
    perm_logger.info("--- Starte Prüfung der Konfigurations-Standardwerte ---")

    install_path = get_install_path()
    config_file = os.path.join(install_path, "e3dc.config.txt")

    if not os.path.exists(config_file):
        print(f"✗ Konfigurationsdatei {config_file} nicht gefunden. Prüfung übersprungen.")
        perm_logger.warning(f"e3dc.config.txt nicht gefunden, Prüfung der Standardwerte übersprungen.")
        return True # Kein Fehler, da die Datei vielleicht erst später erstellt wird.

    defaults_to_check = {
        "show_forecast": "0",
        "wbcostpowers": "7.2, 11.0",
        "darkmode": "1",
        "pvatmosphere": "0.815",
        "check_updates": "0"
    }

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        existing_keys = {line.split('=', 1)[0].strip().lower() for line in content.splitlines() if '=' in line and not line.strip().startswith('#')}

        missing_keys_to_add = []
        for key, value in defaults_to_check.items():
            if key.lower() not in existing_keys:
                missing_keys_to_add.append(f"{key} = {value}")
                print(f"✗ Variable '{key}' fehlt in e3dc.config.txt.")
                perm_logger.warning(f"Variable '{key}' fehlt in e3dc.config.txt.")

        if not missing_keys_to_add:
            print("✓ Alle notwendigen UI-Konfigurationsvariablen sind vorhanden.\n")
            perm_logger.info("Alle UI-Konfigurationsvariablen vorhanden.")
            return True
        else:
            print("\n→ Füge fehlende Variablen am Ende der Datei hinzu...")
            if not content.endswith('\n'):
                content += '\n'
            content += "\n# --- Automatisch hinzugefügte UI-Parameter ---\n"
            content += "\n".join(missing_keys_to_add) + "\n"
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(content)
            print("\n✓ Konfigurationsdatei aktualisiert.\n")
            return True
    except Exception as e:
        print(f"✗ Fehler beim Lesen oder Schreiben von {config_file}: {e}")
        perm_logger.error(f"Fehler beim Prüfen/Setzen der Config-Defaults: {e}")
        return False

def check_config_duplicates():
    """Prüft e3dc.config.txt auf doppelte Einträge (case-insensitive) und entfernt Duplikate (behält das erste)."""
    print("\n=== Konfigurations-Duplikat-Prüfung ===\n")
    perm_logger.info("--- Starte Prüfung auf Konfigurations-Duplikate ---")

    install_path = get_install_path()
    config_file = os.path.join(install_path, "e3dc.config.txt")

    if not os.path.exists(config_file):
        print(f"✓ Konfigurationsdatei {config_file} nicht gefunden. Übersprungen.")
        return True

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        seen_keys = set()
        new_lines = []
        duplicates_found = False
        removed_count = 0

        for line in lines:
            stripped = line.strip()
            # Kommentare und Leerzeilen behalten
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            
            if "=" in stripped:
                parts = stripped.split("=", 1)
                key = parts[0].strip().lower()
                
                if key in seen_keys:
                    print(f"  ✗ Duplikat gefunden und entfernt: {parts[0].strip()} (Zeile: {stripped})")
                    perm_logger.warning(f"Duplikat entfernt: {parts[0].strip()}")
                    duplicates_found = True
                    removed_count += 1
                    continue # Zeile überspringen (löschen)
                else:
                    seen_keys.add(key)
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if duplicates_found:
            print(f"\n→ Entferne {removed_count} Duplikate (behalte jeweils das erste)...")
            with open(config_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print("✓ Konfigurationsdatei bereinigt.\n")
            perm_logger.info(f"Konfigurationsdatei bereinigt, {removed_count} Duplikate entfernt.")
            return True
        else:
            print("✓ Keine Duplikate gefunden.\n")
            perm_logger.info("Keine Duplikate in Konfiguration gefunden.")
            return True

    except Exception as e:
        print(f"✗ Fehler bei der Duplikat-Prüfung: {e}")
        perm_logger.error(f"Fehler bei Duplikat-Prüfung: {e}")
        return False

def run_permissions_wizard(headless=False):
    """Hauptlogik für Rechteprüfung und -korrektur."""
    # Als erstes: Bereinige root-eigene Dateien falls vorhanden
    cleanup_success = cleanup_root_owned_files()
    if not cleanup_success:
        print("⚠ Warnung: Cleanup von root-Dateien hatte Fehler")
        log_warning("permissions", "Cleanup von root-Dateien hatte Fehler")

    # NEU: Auf Duplikate prüfen und bereinigen (bevor Defaults geprüft werden)
    check_config_duplicates()

    # NEU: Standardwerte in der Konfiguration prüfen und setzen
    config_defaults_success = check_and_set_config_defaults()
    if not config_defaults_success:
        print("⚠ Warnung: Prüfung der Konfigurations-Standardwerte hatte Fehler")
        log_warning("permissions", "Prüfung der Konfigurations-Standardwerte hatte Fehler")

    issues = check_permissions()
    wp_issues = check_webportal_permissions()
    file_issues = check_file_permissions()
    cron_issues = check_cronjobs()
    sudo_issues = check_sudoers_permissions()
    service_issues = check_services()
    legacy_issues = check_legacy_autostart()

    has_issues = bool(issues) or bool(wp_issues) or bool(file_issues) or bool(cron_issues) or bool(sudo_issues) or bool(service_issues) or bool(legacy_issues)
    if not has_issues:
        print("\n✓ Alle Berechtigungen, Cronjobs und Services sind korrekt.\n")
        perm_logger.info("✓ Prüfung bestanden: Keine Probleme gefunden.")
        log_task_completed("Rechte prüfen & korrigieren", details="Alle Checks OK")
        return

    print("\n⚠ Probleme gefunden.")
    perm_logger.warning(f"⚠ Probleme erkannt: {len(issues)} Verz., {len(wp_issues)} Web, {len(file_issues)} Dateien, {len(cron_issues)} Cronjobs, {len(sudo_issues)} Sudoers, {len(service_issues)} Services, {len(legacy_issues)} Legacy")
    
    if not headless:
        choice = input("Automatisch korrigieren? (j/n): ").strip().lower()
        if choice != "j":
            print("✗ Korrektur übersprungen.\n")
            perm_logger.warning("✗ Korrektur vom Benutzer übersprungen.")
            log_warning("permissions", "Korrektur vom Benutzer übersprungen")
            return
    else:
        print("→ Automatische Korrektur (Headless-Modus)...")

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
    if service_issues:
        success = fix_services(service_issues)
        all_success = all_success and success
    if legacy_issues:
        success = fix_legacy_autostart(legacy_issues)
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
