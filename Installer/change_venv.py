from .core import CAT_ENV, register_command
from .installer_config import get_venv_name
from .utils import setup_venv

def change_venv_name():
    """Wechselt erst nach vollständig validierter Zielumgebung atomar das venv."""
    print("\n=== Python venv Namen ändern ===\n")

    try:
        current_name = get_venv_name()
    except Exception as exc:
        print(f"✗ Aktuelle venv-Bindung ist nicht vertrauenswürdig: {exc}")
        return False

    print(f"Aktueller Name: {current_name}")
    new_name = input(f"Neuen Namen eingeben [{current_name}]: ").strip()
    if not new_name:
        new_name = current_name

    if new_name == current_name:
        print("→ Name ist unverändert.\n")
        return True

    if setup_venv(
        show_header=True,
        requested_venv_name=new_name,
    ) is not True:
        print(
            "✗ Das neue venv wurde nicht vollständig bestätigt; "
            "die bisherige Metadatenbindung bleibt erhalten."
        )
        return False

    print(f"\n✓ venv-Bindung atomar auf '{new_name}' umgestellt.")
    print(f"Hinweis: Der alte Ordner '{current_name}' wurde nicht gelöscht.")
    print("Du kannst ihn nach einer separaten Prüfung manuell entfernen.")
    return True

register_command("22", "Python venv Namen ändern", change_venv_name, sort_order=22, category=CAT_ENV)
