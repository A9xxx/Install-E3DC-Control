from .core import CAT_ENV, register_command
from .utils import prepare_system_packages_for_snapshot


def prepare_system_packages_menu():
    """Installiert die Paketbasis fuer Container-/VM-Snapshots und beendet."""
    ok = prepare_system_packages_for_snapshot(use_venv=True)
    if ok is False:
        return False
    print("→ Paketbasis vorbereitet. Installer wird beendet.\n")
    raise SystemExit(0)


register_command(
    "25",
    "Systempakete für Snapshot vorbereiten & beenden",
    prepare_system_packages_menu,
    sort_order=25,
    category=CAT_ENV,
)
