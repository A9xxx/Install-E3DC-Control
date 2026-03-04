import os
import shutil
import subprocess
import tempfile

from .core import register_command
from .utils import apt_install, pip_install, run_command, command_exists
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
system_logger = get_or_create_logger("system")

PYTHON_PACKAGES = ["plotly>=5.0", "pandas-stubs", "pandas", "pytz", "matplotlib", "paho-mqtt", "requests", "pymodbus"]

def get_venv_name():
    return load_config().get("venv_name", ".venv_e3dc")

def setup_venv(show_header=False):
    """Richtet das Python Virtual Environment ein."""
    if show_header:
        print("\n=== Python Virtual Environment einrichten ===\n")
    
    install_user = get_install_user()
    venv_name = get_venv_name() or ".venv_e3dc"
    venv_path = os.path.join(get_home_dir(install_user), venv_name)

    # Altes venv im E3DC-Control Ordner bereinigen (Migration)
    old_venv_path = os.path.join(INSTALL_PATH, venv_name)
    if os.path.exists(old_venv_path) and os.path.isdir(old_venv_path):
        if os.path.abspath(old_venv_path) != os.path.abspath(venv_path):
            print(f"→ Entferne veraltetes venv: {old_venv_path}")
            try:
                shutil.rmtree(old_venv_path)
                print("✓ Altes venv bereinigt.")
            except Exception as e:
                print(f"⚠ Konnte altes venv nicht löschen: {e}")
    
    print(f"→ Ziel: {venv_path}")
    
    if not os.path.exists(venv_path):
        print("→ Erstelle venv…")
        # Erstelle venv mit Zugriff auf System-Pakete (für apt-installierte Module wie RPi.GPIO falls nötig)
        res = run_command(f"sudo -u {install_user} python3 -m venv {venv_path} --system-site-packages")
        if res['success']:
            print("✓ venv erstellt.")
            system_logger.info(f"Virtual Environment erstellt: {venv_path}")
        else:
            print(f"✗ Fehler beim Erstellen: {res['stderr']}")
            return False
    else:
        print("✓ venv existiert bereits.")
    
    venv_pip = os.path.join(venv_path, "bin", "pip")
    
    print("\n→ Installiere Python-Pakete in venv…\n")
    system_logger.info(f"Installiere {len(PYTHON_PACKAGES)} Python-Pakete in {venv_path}.")
    
    for pkg in PYTHON_PACKAGES:
        # Paketname extrahieren (alles vor >, <, =)
        pkg_name = pkg.split('>')[0].split('=')[0].split('<')[0]
        
        # Prüfen ob Paket installiert ist
        check_cmd = f"sudo -u {install_user} {venv_pip} show {pkg_name}"
        check_res = run_command(check_cmd)
        
        if check_res['success']:
            # Bei Version-Constraint sicherheitshalber install ausführen (Update/Check)
            if any(c in pkg for c in ['>', '=', '<']):
                run_command(f"sudo -u {install_user} {venv_pip} install {pkg}")
                print(f"  ✓ {pkg} (geprüft)")
            else:
                print(f"  ✓ {pkg} bereits installiert")
        else:
            print(f"  → Installiere {pkg}...")
            run_command(f"sudo -u {install_user} {venv_pip} install {pkg}")
    
    if show_header:
        print("\n✓ Python-Umgebung eingerichtet.\n")
        log_task_completed("Python venv eingerichtet")
    return True

def list_venv_packages():
    """Listet installierte Pakete im venv auf."""
    print("\n=== Python venv Pakete ===\n")
    
    install_user = get_install_user()
    venv_name = get_venv_name() or ".venv_e3dc"
    venv_pip = os.path.join(get_home_dir(install_user), venv_name, "bin", "pip")
    
    if not os.path.exists(venv_pip):
        print("✗ Kein venv gefunden.")
        return

    res = run_command(f"sudo -u {install_user} {venv_pip} list")
    if res['success']:
        print(res['stdout'])
    else:
        print(f"✗ Fehler: {res['stderr']}")
    print()

def install_global_python_packages():
    """Installiert Python-Pakete global (System)."""
    print("\n→ Installiere Python-Pakete systemweit (global)…")
    system_logger.info(f"Installiere {len(PYTHON_PACKAGES)} Python-Pakete global.")
    
    for pkg in PYTHON_PACKAGES:
        pip_install(pkg)

def install_system_packages(use_venv=True):
    """Installiert alle notwendigen Systempakete."""
    print("\n=== Systempakete installieren ===\n")
    system_logger.info("Starte Installation der System- und Python-Pakete.")

    packages = [
        "curl", "jq", "python3-bs4", "git", "screen",
        "apache2", "php", "python3", "python3-pip", "python3-venv",
        "python3-plotly", "libjpeg-dev", "zlib1g-dev",
        "libcurl4-openssl-dev", "libssl-dev",
        "libmosquitto-dev", "libjsoncpp-dev",
        "libsqlite3-dev", "build-essential", "cmake"
    ]

    print("→ Installiere Systempakete…\n")
    system_logger.info(f"Installiere {len(packages)} Systempakete.")
    for pkg in packages:
        apt_install(pkg)

    # Python Umgebung einrichten
    if use_venv:
        setup_venv(show_header=False)
    else:
        install_global_python_packages()

    print("\n✓ Systempakete vollständig installiert.\n")
    system_logger.info("Installation der Pakete abgeschlossen.")
    log_task_completed("Systempakete installieren")


def install_e3dc_control():
    """Klont und kompiliert E3DC-Control."""
    print("\n=== E3DC-Control installieren ===\n")
    system_logger.info("Starte Installation von E3DC-Control (Klonen & Kompilieren).")

    # NEU: Prüfen, ob git überhaupt installiert ist
    if not command_exists("git"):
        print("✗ Git ist nicht installiert. Breche ab.")
        print("  Bitte führe zuerst 'Systempakete installieren' aus.")
        log_error("system", "Git ist nicht installiert. Klonen von E3DC-Control abgebrochen.")
        return False

    service_was_stopped = False
    temp_venv_backup = None

    if os.path.exists(INSTALL_PATH):
        print("⚠ E3DC-Control existiert bereits.")
        choice = input("Überschreiben? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Schritt übersprungen, verwende vorhandene Installation.\n")
            system_logger.warning("Installation von E3DC-Control übersprungen, da Verzeichnis bereits existiert.")
            return True
        
        if os.path.exists("/etc/systemd/system/e3dc.service"):
            print("→ Stoppe E3DC-Control Service…")
            run_command("sudo systemctl stop e3dc")
            service_was_stopped = True
        
        # VENV RETTUNG: Prüfen ob venv im Ordner liegt und sichern
        venv_name = get_venv_name()
        if venv_name:
            possible_venv = os.path.join(INSTALL_PATH, venv_name)
            if os.path.exists(possible_venv) and os.path.isdir(possible_venv):
                print(f"  ℹ️  Sichere venv vor dem Löschen: {possible_venv}")
                try:
                    temp_venv_backup = os.path.join(tempfile.gettempdir(), f"{venv_name}_backup_{os.getpid()}")
                    if os.path.exists(temp_venv_backup):
                        shutil.rmtree(temp_venv_backup)
                    shutil.move(possible_venv, temp_venv_backup)
                except Exception as e:
                    print(f"  ⚠ Konnte venv nicht sichern: {e}")
            
        print("→ Entferne altes Verzeichnis…")
        try:
            shutil.rmtree(INSTALL_PATH, ignore_errors=True)
            system_logger.info(f"Altes Verzeichnis entfernt: {INSTALL_PATH}")
        except Exception as e:
            print(f"✗ Fehler beim Löschen: {e}\n")
            log_error("system", f"Fehler beim Löschen des alten Verzeichnisses: {e}", e)
            return False

    print("→ Klone Repository…")
    install_user = get_install_user()
    result = run_command(
        f"sudo -u {install_user} git clone https://github.com/Eba-M/E3DC-Control.git {INSTALL_PATH}",
        timeout=120
    )

    if not result['success']:
        print(f"✗ Git Clone fehlgeschlagen: {result['stderr']}\n")
        log_error("system", f"Git Clone fehlgeschlagen: {result['stderr']}")
        return False
    system_logger.info("Repository erfolgreich geklont.")

    # VENV WIEDERHERSTELLUNG
    if temp_venv_backup and os.path.exists(temp_venv_backup):
        target_venv = os.path.join(INSTALL_PATH, venv_name)
        print(f"→ Stelle venv wieder her: {target_venv}")
        try:
            if os.path.exists(target_venv):
                shutil.rmtree(target_venv)
            shutil.move(temp_venv_backup, target_venv)
            
            # Rechte sicherstellen (install_user)
            run_command(f"chown -R {install_user}:{install_user} {target_venv}")
        except Exception as e:
            print(f"  ⚠ Konnte venv nicht wiederherstellen: {e}")
            log_error("system", f"Konnte venv nicht wiederherstellen: {e}", e)

    print("→ Kompiliere…")
    # Venv nutzen falls vorhanden
    venv_name = get_venv_name()
    venv_act = os.path.join(INSTALL_PATH, venv_name, "bin", "activate") if venv_name else ""
    make_cmd = "make"
    if venv_name and os.path.exists(venv_act):
        make_cmd = f"source {venv_act} && make"
        print("  (in venv Umgebung)")
    result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && {make_cmd}'", timeout=300)

    if not result['success']:
        print(f"✗ Kompilierung fehlgeschlagen: {result['stderr']}\n")
        log_error("system", f"Kompilierung fehlgeschlagen: {result['stderr']}")
        return False
    system_logger.info("Kompilierung erfolgreich.")

    # Setze Ausführungsrechte
    try:
        os.chmod(os.path.join(INSTALL_PATH, "E3DC-Control"), 0o755)
        system_logger.info("Ausführungsrechte für E3DC-Control Binary gesetzt.")
    except Exception:
        pass

    if service_was_stopped:
        print("→ Starte E3DC-Control Service…")
        run_command("sudo systemctl start e3dc")

    print("✓ E3DC-Control installiert.\n")
    log_task_completed("E3DC-Control installieren")
    return True


def system_packages_menu():
    """Menü für Systempakete."""
    install_system_packages()


def reinstall_menu():
    """Menü für Neuinstallation."""
    install_e3dc_control()


def setup_venv_menu():
    """Menü für venv Einrichtung."""
    setup_venv(show_header=True)

register_command("3", "Systempakete installieren", system_packages_menu, sort_order=30)
register_command("4", "E3DC-Control neu installieren", reinstall_menu, sort_order=40)
register_command("22", "Python venv einrichten (Reparatur)", setup_venv_menu, sort_order=220)
register_command("23", "Python venv Pakete anzeigen", list_venv_packages, sort_order=230)
