import importlib
import pkgutil
import os

from .task_executor import safe_menu_action
from .installer_config import get_install_user
from .logging_manager import setup_installation_loggers

# Basis: alle Module im Installer-Paket durchsuchen
PACKAGE_NAME = __name__.rsplit(".", 1)[0]  # "Installer"


class Command:
    def __init__(self, key, label, func, sort_order=100):
        self.key = key          # z.B. "1"
        self.label = label      # z.B. "Rechte prüfen"
        self.func = func        # Callable
        self.sort_order = sort_order
    
    def __repr__(self):
        return f"Command({self.key}, {self.label})"


COMMANDS = []
_modules_loaded = False


def register_command(key, label, func, sort_order=100):
    """Registriert einen Befehl im Menü."""
    COMMANDS.append(Command(key, label, func, sort_order))


def auto_discover_modules():
    """Lädt alle Module im Installer-Paket und lässt sie ihre Commands registrieren."""
    global _modules_loaded
    
    if _modules_loaded:
        return
    
    package_path = os.path.dirname(__file__)

    for _, module_name, is_pkg in pkgutil.iter_modules([package_path]):
        if is_pkg or module_name in ("core", "__init__"):
            continue

        try:
            full_name = f"{PACKAGE_NAME}.{module_name}"
            importlib.import_module(full_name)
        except Exception as e:
            print(f"⚠ Warnung: Konnte Modul '{module_name}' nicht laden: {e}")
    
    _modules_loaded = True


def get_menu_commands():
    """Gibt eine sortierte Liste der registrierten Commands zurück."""
    if not _modules_loaded:
        auto_discover_modules()
    
    # Sortierung nach sort_order, dann Label
    return sorted(COMMANDS, key=lambda c: (c.sort_order, c.label.lower()))


def print_menu():
    """Druckt das Menü aus."""
    print("\n" + "=" * 40)
    print("    E3DC-Control Installer")
    print("=" * 40 + "\n")

    commands = get_menu_commands()

    for cmd in commands:
        print(f"  {cmd.key}) {cmd.label}")
    
    print(f"  q) Beenden")
    print()


def run_main_menu(restart_callback=None):
    """Hauptmenü-Loop."""
    auto_discover_modules()
    setup_installation_loggers()
    install_user = get_install_user()
    
    while True:
        print_menu()
        choice = input(f"Auswahl ({install_user}): ").strip().lower()

        if choice == "q":
            print("→ Beende Installer.\n")
            break

        commands = get_menu_commands()
        matched = [c for c in commands if c.key == choice]
        
        if not matched:
            print("✗ Ungültige Auswahl.\n")
            continue

        cmd = matched[0]
        safe_menu_action(cmd.key, cmd.label, cmd.func)

        # Optional: nach Updates/Rollback neu starten
        if restart_callback and choice in ("2",):  # z.B. nur nach Update
            print("→ Neuladen des Menüs…\n")
            restart_callback()
