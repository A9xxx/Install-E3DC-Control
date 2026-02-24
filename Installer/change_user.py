import os
from .core import register_command
from .installer_config import save_config, get_install_user

def change_install_user():
    """Installationsbenutzer ändern"""
    print("\n=== Installationsbenutzer ändern ===\n")
    current_user = get_install_user()
    print(f"Aktueller Installationsbenutzer: {current_user}")
    new_user = input("Neuen Installationsbenutzer eingeben: ").strip()
    if not new_user:
        print("✗ Kein Benutzer eingegeben. Abbruch.\n")
        return
    if new_user == current_user:
        print("→ Benutzer ist bereits gesetzt. Keine Änderung.\n")
        return
    save_config({"install_user": new_user})
    print(f"✓ Installationsbenutzer geändert auf: {new_user}\n")

register_command("18", "Installationsbenutzer ändern", change_install_user, sort_order=180)
