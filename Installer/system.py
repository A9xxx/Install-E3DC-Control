import os
import shutil
import subprocess

from .core import register_command
from .utils import apt_install, pip_install, run_command, command_exists
from .installer_config import get_install_path, get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
system_logger = get_or_create_logger("system")


def install_system_packages():
    """Installiert alle notwendigen Systempakete."""
    print("\n=== Systempakete installieren ===\n")
    system_logger.info("Starte Installation der System- und Python-Pakete.")

    packages = [
        "curl", "jq", "python3-bs4", "git", "screen",
        "apache2", "php", "python3", "python3-pip",
        "python3-plotly", "libjpeg-dev", "zlib1g-dev",
        "libcurl4-openssl-dev", "libssl-dev",
        "libmosquitto-dev", "libjsoncpp-dev",
        "libsqlite3-dev", "build-essential", "cmake"
    ]

    print("→ Installiere Systempakete…\n")
    system_logger.info(f"Installiere {len(packages)} Systempakete.")
    for pkg in packages:
        apt_install(pkg)

    python_packages = ["plotly>=5.0", "pandas-stubs", "pandas", "pytz", "matplotlib"]
    print("\n→ Installiere Python-Pakete…\n")
    system_logger.info(f"Installiere {len(python_packages)} Python-Pakete.")
    for pkg in python_packages:
        pip_install(pkg)

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

    if os.path.exists(INSTALL_PATH):
        print("⚠ E3DC-Control existiert bereits.")
        choice = input("Überschreiben? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Schritt übersprungen, verwende vorhandene Installation.\n")
            system_logger.warning("Installation von E3DC-Control übersprungen, da Verzeichnis bereits existiert.")
            return True
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

    print("→ Kompiliere…")
    result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && make'", timeout=300)

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

    print("✓ E3DC-Control installiert.\n")
    log_task_completed("E3DC-Control installieren")
    return True


def system_packages_menu():
    """Menü für Systempakete."""
    install_system_packages()


def reinstall_menu():
    """Menü für Neuinstallation."""
    install_e3dc_control()


register_command("3", "Systempakete installieren", system_packages_menu, sort_order=30)
register_command("4", "E3DC-Control neu installieren", reinstall_menu, sort_order=40)
