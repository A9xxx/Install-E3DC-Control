import importlib
import pkgutil
import os

from .task_executor import safe_menu_action
from .installer_config import get_install_user
from .logging_manager import setup_installation_loggers, print_installation_summary

# Basis: alle Module im Installer-Paket durchsuchen
PACKAGE_NAME = __name__.rsplit(".", 1)[0]  # "Installer"

# Kategorien-Definitionen
CAT_CORE = "Kernsystem & Update"
CAT_ENV = "Umgebung & Python"
CAT_DOCKER = "Docker Migration & Verwaltung"
CAT_EXTENSIONS = "Erweiterungen & Smart Home"
CAT_OTHER = "Sonstiges"

# Mapping der IDs zu Kategorien (Fallback, falls beim Register nicht angegeben)
CATEGORY_MAP = {
    "11": CAT_CORE, "12": CAT_CORE, "13": CAT_CORE, "14": CAT_CORE, "15": CAT_CORE,
    "20": CAT_ENV, "21": CAT_ENV, "22": CAT_ENV, "23": CAT_ENV,
    "24": CAT_ENV, "25": CAT_ENV, "26": CAT_ENV, "27": CAT_ENV,
    "28": CAT_ENV, "29": CAT_ENV,
    "31": CAT_DOCKER, "32": CAT_DOCKER,
    "41": CAT_EXTENSIONS, "42": CAT_EXTENSIONS, "43": CAT_EXTENSIONS,
    "44": CAT_EXTENSIONS, "45": CAT_EXTENSIONS, "46": CAT_EXTENSIONS,
    "47": CAT_EXTENSIONS, "48": CAT_EXTENSIONS, "49": CAT_EXTENSIONS
}

# Reihenfolge der Kategorien im Hauptmenü
CATEGORY_ORDER = [
    CAT_CORE,
    CAT_ENV,
    CAT_DOCKER,
    CAT_EXTENSIONS,
    CAT_OTHER,
]

# Das Konsolenmenü bleibt bewusst klein: Alles, was im Alltag gebraucht wird,
# liegt direkt im Hauptmenü. Erweiterungen und Sonderfälle liegen gesammelt im
# Expertenmenü, damit Neuinstallationen nicht in alten Modulnummern landen.
MAIN_MENU_SLOTS = [
    ("1", "11", "Installation / Update"),
    ("2", "20", "Systemstatus anzeigen"),
    ("3", "24", "Rechte prüfen & korrigieren"),
    ("4", "99", "Notfallmodus / System reparieren"),
    ("5", "12", "Rollback auf Git-Stand"),
    ("6", "13", "Backup erstellen / verwalten"),
    ("7", None, "Expertenmenü"),
    ("8", "25", "Systempakete vorbereiten"),
    ("9", "29", "Deinstallation"),
]

MAIN_MENU_KEYS = {key for _, key, _ in MAIN_MENU_SLOTS if key}

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


def _install_matter_bridge_lazy(headless=False):
    """Lädt die optionale Matter-Installation erst bei ausdrücklicher Auswahl."""
    try:
        module = importlib.import_module(f"{PACKAGE_NAME}.install_matter")
        installer = getattr(module, "install_matter_bridge")
    except (AttributeError, ImportError) as exc:
        print(
            "✗ Matter-Installation kann nicht geladen werden. "
            "Bitte den E3DC-Control-Installer zuerst vollständig aktualisieren."
        )
        print(f"  Technisches Detail: {exc}")
        return False

    return installer(headless=headless)


# Matter ist eine optionale Erweiterung. Ihr Abhängigkeitsbaum darf deshalb
# weder den normalen Installerstart noch ein Core-Update blockieren.
register_command(
    "45",
    "Smart Home Matter Bridge",
    _install_matter_bridge_lazy,
    sort_order=45,
)


def auto_discover_modules():
    """Lädt alle Module im Installer-Paket und lässt sie ihre Commands registrieren."""
    global _modules_loaded

    if _modules_loaded:
        return

    package_path = os.path.dirname(__file__)

    # Legacy-Setup-Skripte ignorieren wir; normale Einrichtung läuft über
    # Installation/Update und danach über WebUI bzw. Config-Editor.
    IGNORE_MODULES = (
        "core", "__init__", "e3dc_mqtt_hub", "e3dc_websocket",
        "bluelink_client", "sqlite_archiver", "notification_manager", "ha_manager",
        "ml_predictor", "generate_vapid", "wallbox_manager", "energy_manager", "idm_live",
        "heizstab_manager", "climate_live", "climate_control", "stiebel", "e3dc_live",
        "storage_manager", "storage_manager_legacy", "storage_simulator", "epex_manager",

        # Obsolete Installer Module verbannen (Echte Dateinamen!):
        "install_all", "config_wizard", "create_config", "strompreis_wizard",
        "system", "ramdisk", "self_update", "change_user", "diagrammphp", "e3dc_setup",
        # Lokale Entwicklungs- und Reparaturwerkzeuge sind keine Bedienbefehle.
        "force_discharge", "recover_history_rscp", "repair_db_inversion", "permissions_helper",
        # Integrations-Guide: importiert get_registered_commands (existiert nicht -> Warnung)
        "INTEGRATIONS_GUIDE",
        # Optionaler Abhängigkeitsbaum: nur über _install_matter_bridge_lazy laden.
        "install_matter",
    )

    for _, module_name, is_pkg in pkgutil.iter_modules([package_path]):
        # Prüfe strikt, ob das Modul in der Ignore-Liste steht oder durch Auto-Discovery rausfällt
        # Module die mit _ beginnen (private, test, backup) niemals importieren!
        if (is_pkg
                or module_name in IGNORE_MODULES
                or module_name.startswith("gui_")
                or module_name.startswith("_")):
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


def print_main_menu(commands_by_key):
    """Druckt das flache Hauptmenü."""
    print("\n" + "=" * 40)
    print("    E3DC-Control Installer")
    print("=" * 40 + "\n")

    for slot, command_key, label in MAIN_MENU_SLOTS:
        if command_key is None:
            print(f"  {slot}) {label}")
            continue
        if command_key in commands_by_key:
            print(f"  {slot}) {label}")

    print(f"  q) Beenden")
    print()


def print_expert_menu(commands):
    """Druckt das einzige Untermenü für Erweiterungen und Spezialfälle."""
    print("\n" + "-" * 40)
    print("    Expertenmenü")
    print("-" * 40 + "\n")

    current_category = None
    for cmd in commands:
        if cmd.category != current_category:
            current_category = cmd.category
            print(f"  {current_category}")
        print(f"    {cmd.key}) {cmd.label}")

    print(f"\n  q) Zurück zum Hauptmenü")
    print()


def _commands_by_key(commands):
    by_key = {}
    for cmd in commands:
        by_key.setdefault(cmd.key, cmd)
    return by_key


def _expert_commands(commands):
    return sorted(
        [cmd for cmd in commands if cmd.key not in MAIN_MENU_KEYS],
        key=lambda c: (
            CATEGORY_ORDER.index(c.category) if c.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
            c.sort_order,
            c.label.lower(),
        ),
    )


def _run_command(cmd, restart_callback=None):
    success = safe_menu_action(cmd.key, cmd.label, cmd.func)
    if success is not False and restart_callback and cmd.key in ("11", "12"):
        print("→ Neuladen des Menüs…\n")
        restart_callback()
    input("Drücke ENTER um fortzufahren...")
    return success is not False


def run_main_menu(restart_callback=None):
    """Hauptmenü-Loop mit genau einem Expertenmenü."""
    auto_discover_modules()
    setup_installation_loggers()
    install_user = get_install_user()

    commands = get_menu_commands()

    commands_by_key = _commands_by_key(commands)
    expert_commands = _expert_commands(commands)
    current_view = "main"
    failed_action = False

    while True:
        if current_view == "main":
            print_main_menu(commands_by_key)
            choice = input(f"Auswahl ({install_user}): ").strip().lower()

            if choice == "q":
                print("→ Beende Installer.\n")
                break
            elif choice in ("7", "e", "experte", "experten"):
                current_view = "expert"
                continue

            target_key = None
            for slot, command_key, _ in MAIN_MENU_SLOTS:
                if choice == slot or (command_key and choice == command_key):
                    target_key = command_key
                    break

            target = commands_by_key.get(target_key or "")
            if target:
                if not _run_command(target, restart_callback):
                    failed_action = True
            else:
                print("✗ Ungültige Auswahl.\n")

        else:
            print_expert_menu(expert_commands)

            choice = input(f"Befehl ({install_user}): ").strip().lower()

            if choice == "q":
                current_view = "main"
                continue

            matched = [c for c in expert_commands if c.key == choice]

            if matched:
                if not _run_command(matched[0], restart_callback):
                    failed_action = True
            else:
                print("✗ Ungültige Auswahl.\n")

    # Wenn die Haupt-Schleife beendet wurde (mit 'q'), zeige die Zusammenfassung
    print_installation_summary()
    return not failed_action
