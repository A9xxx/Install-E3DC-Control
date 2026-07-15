#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys

def main():
    silent = "--silent" in sys.argv
    is_check = "--check" in sys.argv
    is_fix_permissions = "--fix-permissions" in sys.argv
    
    if not silent and not is_check and not is_fix_permissions:
        print("="*60)
        print(" 🚀 WILLKOMMEN BEIM V4 UPGRADE 🚀")
        print("="*60)
        print("\nE3DC-Control wurde massiv auf Architektur V4 aktualisiert!")
        print("Das alte 'self_update.py' Skript wurde durch unser neues und")
        print("sicheres Installer-Menü ersetzt.\n")
    
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
        
    try:
        if is_check:
            from Installer.update import check_for_updates
            missing = check_for_updates(repo_root)
            if missing == 0:
                print("System ist aktuell.")
            elif missing is None:
                print("Fehler bei der Update-Pruefung.")
            else:
                print(f"{missing} Commits verfuegbar.")
        elif is_fix_permissions:
            from Installer.permissions import run_permissions_wizard
            ok = run_permissions_wizard(headless=True)
            sys.exit(0 if ok is not False else 1)
        else:
            if not silent:
                print("Starte den V4 Upgrade & Update Prozess via installer_main.py...\n")
                sys.stdout.flush()
            from Installer.update import update_e3dc
            update_e3dc(headless=True)
    except Exception as e:
        print(f"Fehler beim Ausfuehren von self_update.py: {e}")
        if not silent:
            print("\nBitte starten Sie den Installer manuell:")
            print("  python3 installer_main.py")

if __name__ == "__main__":
    main()
