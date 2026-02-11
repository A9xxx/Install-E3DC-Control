import os
import shutil
import subprocess

from .core import register_command
from .utils import apt_install, pip_install, run_command

INSTALL_PATH = "/home/pi/E3DC-Control"


def install_system_packages():
    """Installiert alle notwendigen Systempakete."""
    print("\n=== Systempakete installieren ===\n")

    packages = [
        "curl", "jq", "python3-bs4", "git", "screen",
        "apache2", "php", "python3", "python3-pip",
        "python3-plotly", "libjpeg-dev", "zlib1g-dev",
        "libcurl4-openssl-dev", "libssl-dev",
        "libmosquitto-dev", "libjsoncpp-dev",
        "libsqlite3-dev", "build-essential", "cmake"
    ]

    print("→ Installiere Systempakete…\n")
    for pkg in packages:
        apt_install(pkg)

    python_packages = ["plotly", "pandas-stubs", "pytz", "matplotlib"]
    print("\n→ Installiere Python-Pakete…\n")
    for pkg in python_packages:
        pip_install(pkg)

    print("\n✓ Systempakete vollständig installiert.\n")


def install_e3dc_control():
    """Klont und kompiliert E3DC-Control."""
    print("\n=== E3DC-Control installieren ===\n")

    if os.path.exists(INSTALL_PATH):
        print("⚠ E3DC-Control existiert bereits.")
        choice = input("Überschreiben? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Installation abgebrochen.\n")
            return False
        print("→ Entferne altes Verzeichnis…")
        try:
            shutil.rmtree(INSTALL_PATH, ignore_errors=True)
        except Exception as e:
            print(f"✗ Fehler beim Löschen: {e}\n")
            return False

    print("→ Klone Repository…")
    result = run_command(
        "git clone https://github.com/Eba-M/E3DC-Control.git {}".format(INSTALL_PATH),
        timeout=120
    )

    if not result['success']:
        print(f"✗ Git Clone fehlgeschlagen: {result['stderr']}\n")
        return False

    print("→ Kompiliere…")
    result = run_command(f"cd {INSTALL_PATH} && make", timeout=300)

    if not result['success']:
        print(f"✗ Kompilierung fehlgeschlagen: {result['stderr']}\n")
        return False

    # Setze Ausführungsrechte
    try:
        os.chmod(os.path.join(INSTALL_PATH, "E3DC-Control"), 0o755)
    except Exception:
        pass

    print("✓ E3DC-Control installiert.\n")
    return True


def system_packages_menu():
    """Menü für Systempakete."""
    install_system_packages()


def reinstall_menu():
    """Menü für Neuinstallation."""
    install_e3dc_control()


register_command("6", "Systempakete installieren", system_packages_menu, sort_order=60)
register_command("7", "E3DC-Control neu installieren", reinstall_menu, sort_order=70)
