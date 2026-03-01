import importlib
import pkgutil
import os

from .task_executor import safe_menu_action
from .installer_config import get_install_user
from .logging_manager import setup_installation_loggers

# Basis: alle Module im Installer-Paket durchsuchen
PACKAGE_NAME = __name__.rsplit(".", 1)[0]  # "Installer"

# Kategorien-Definitionen
CAT_INSTALL = "Installation & Update"
CAT_CONFIG = "Konfiguration"
CAT_SYSTEM = "System & Dienste"
CAT_EXTENSIONS = "Erweiterungen"
CAT_BACKUP = "Backup & Recovery"
CAT_OTHER = "Sonstiges"

# Mapping der IDs zu Kategorien (Fallback, falls beim Register nicht angegeben)
CATEGORY_MAP = {
    "1": CAT_INSTALL, "3": CAT_INSTALL, "4": CAT_INSTALL, "5": CAT_INSTALL, "18": CAT_INSTALL, "20": CAT_INSTALL,
    "7": CAT_CONFIG, "8": CAT_CONFIG, "9": CAT_CONFIG, "19": CAT_CONFIG,
    "2": CAT_SYSTEM, "11": CAT_SYSTEM, "12": CAT_SYSTEM, "14": CAT_SYSTEM, "21": CAT_SYSTEM, "99": CAT_SYSTEM,
    "10": CAT_EXTENSIONS, "13": CAT_EXTENSIONS, "15": CAT_EXTENSIONS,
    "6": CAT_BACKUP, "16": CAT_BACKUP, "17": CAT_BACKUP
}

# Reihenfolge der Kategorien im Hauptmenü
CATEGORY_ORDER = [
    CAT_INSTALL,
    CAT_CONFIG,
    CAT_SYSTEM,
    CAT_EXTENSIONS,
    CAT_BACKUP,
    CAT_OTHER
]

class Command:
    def __init__(self, key, label, func, sort_order=100, category=None):
        self.key = key          # z.B. "1"
        self.label = label      # z.B. "Rechte prüfen"
        self.func = func        # Callable
        self.sort_order = sort_order
        self.category = category or CAT_OTHER
    
    def __repr__(self):
        return f"Command({self.key}, {self.label})"


COMMANDS = []
_modules_loaded = False


def register_command(key, label, func, sort_order=100, category=None):
    """Registriert einen Befehl im Menü."""
    if category is None:
        category = CATEGORY_MAP.get(key, CAT_OTHER)
    COMMANDS.append(Command(key, label, func, sort_order, category))


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


def print_main_menu(categories):
    """Druckt das Hauptmenü (Kategorien)."""
    print("\n" + "=" * 40)
    print("    E3DC-Control Installer")
    print("=" * 40 + "\n")

    for idx, cat in enumerate(categories, 1):
        print(f"  {idx}) {cat}")
    
    print("\n  a) Alle Befehle anzeigen (Liste)")
    print(f"  s) Befehl suchen")
    print(f"  q) Beenden")
    print()


def print_category_menu(category, commands):
    """Druckt das Untermenü einer Kategorie."""
    print("\n" + "-" * 40)
    print(f"    {category}")
    print("-" * 40 + "\n")

    for cmd in commands:
        print(f"  {cmd.key}) {cmd.label}")
    
    print(f"\n  b) Zurück zum Hauptmenü")
    print()


def print_all_commands_menu(commands):
    """Druckt alle Befehle flach aus (Legacy-Ansicht)."""
    print("\n" + "=" * 40)
    print("    Alle Befehle")
    print("=" * 40 + "\n")
    
    for cmd in commands:
        print(f"  {cmd.key}) {cmd.label}")
    
    print(f"  b) Zurück")
    print(f"  q) Beenden")
    print()


def run_main_menu(restart_callback=None):
    """Hauptmenü-Loop mit Untermenüs."""
    auto_discover_modules()
    setup_installation_loggers()
    install_user = get_install_user()
    
    commands = get_menu_commands()
    
    # Filtere leere Kategorien heraus
    active_categories = [
        cat for cat in CATEGORY_ORDER 
        if any(c.category == cat for c in commands)
    ]
    
    current_view = "main" # main, all, oder category_name
    
    while True:
        if current_view == "main":
            print_main_menu(active_categories)
            choice = input(f"Auswahl ({install_user}): ").strip().lower()
            
            if choice == "q":
                print("→ Beende Installer.\n")
                break
            elif choice == "a":
                current_view = "all"
                continue
            elif choice == "s":
                current_view = "search"
                continue
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_categories):
                    current_view = active_categories[idx]
                    continue
            
            print("✗ Ungültige Auswahl.\n")

        elif current_view == "search":
            print("\n" + "-" * 40)
            print("    Befehl suchen")
            print("-" * 40 + "\n")
            
            term = input("Suchbegriff (leer für Zurück): ").strip().lower()
            if not term:
                current_view = "main"
                continue
            
            matches = [c for c in commands if term in c.label.lower() or term == c.key.lower()]
            
            if not matches:
                print("✗ Keine Treffer.\n")
                continue
            
            print(f"\nTreffer für '{term}':")
            for cmd in matches:
                print(f"  {cmd.key}) {cmd.label} ({cmd.category})")
            print()
            
            sel = input(f"Befehl ausführen (Nummer) oder 'b' für Zurück: ").strip().lower()
            if sel == "b":
                continue
            
            target = next((c for c in matches if c.key == sel), None)
            if target:
                safe_menu_action(target.key, target.label, target.func)
                if restart_callback and target.key in ("2",):
                    print("→ Neuladen des Menüs…\n")
                    restart_callback()
                input("Drücke ENTER um fortzufahren...")
                current_view = "main"
            else:
                print("✗ Ungültige Auswahl.\n")

        else:
            # Untermenü oder Alle
            if current_view == "all":
                view_commands = commands
                print_all_commands_menu(view_commands)
            else:
                view_commands = [c for c in commands if c.category == current_view]
                print_category_menu(current_view, view_commands)

            choice = input(f"Befehl ({install_user}): ").strip().lower()

            if choice == "b":
                current_view = "main"
                continue
            elif choice == "q" and current_view == "all":
                print("→ Beende Installer.\n")
                break
            
            matched = [c for c in view_commands if c.key == choice]
            if not matched:
                # Versuche globalen Match (falls User ID aus anderer Kategorie kennt)
                matched = [c for c in commands if c.key == choice]
            
            if matched:
                cmd = matched[0]
                safe_menu_action(cmd.key, cmd.label, cmd.func)
                
                # Optional: nach Updates/Rollback neu starten
                if restart_callback and choice in ("2",):
                    print("→ Neuladen des Menüs…\n")
                    restart_callback()
                
                # Nach Ausführung im Untermenü bleiben oder zurück?
                # Bleiben ist meist angenehmer für Folgebefehle.
                input("Drücke ENTER um fortzufahren...")
            else:
                print("✗ Ungültige Auswahl.\n")
