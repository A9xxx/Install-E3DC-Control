#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys


def _reject_privileged_web_invocation() -> None:
    """Sperrt alte direkte sudoers-Freigaben für den Kompatibilitäts-Updater."""
    sudo_user = str(os.environ.get("SUDO_USER") or "").strip()
    if os.geteuid() == 0 and sudo_user == "www-data":
        print(
            "Sicherheitssperre: self_update.py darf nicht privilegiert "
            "aus dem Webserverkontext gestartet werden.",
            file=sys.stderr,
        )
        raise SystemExit(126)


_reject_privileged_web_invocation()

SYSTEM_PYTHON = "/usr/bin/python3"


def _ensure_isolated_system_python() -> None:
    """Route release changes through the fixed, isolated system interpreter."""
    current = os.path.realpath(sys.executable)
    expected = os.path.realpath(SYSTEM_PYTHON)
    isolated = bool(getattr(sys.flags, "isolated", 0))
    no_bytecode = bool(getattr(sys.flags, "dont_write_bytecode", 0))
    unbuffered = bool(os.environ.get("PYTHONUNBUFFERED")) or bool(
        getattr(sys.stdout, "write_through", False)
    )
    if current == expected and isolated and no_bytecode and unbuffered:
        return
    if os.geteuid() != 0:
        raise PermissionError(
            "Der Kompatibilitäts-Updater muss als root über den geprüften "
            "Installer-Wrapper gestartet werden."
        )
    os.execv(
        SYSTEM_PYTHON,
        [
            SYSTEM_PYTHON,
            "-I",
            "-B",
            "-u",
            os.path.abspath(__file__),
            *sys.argv[1:],
        ],
    )


def main() -> int:
    silent = "--silent" in sys.argv
    is_check = "--check" in sys.argv
    is_fix_permissions = "--fix-permissions" in sys.argv
    if not is_check and not is_fix_permissions:
        _ensure_isolated_system_python()

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
                return 0
            elif missing is None:
                print("Fehler bei der Update-Prüfung.")
                return 1
            else:
                print(f"{missing} Commits verfügbar.")
                return 0
        elif is_fix_permissions:
            from Installer.permissions import run_permissions_wizard
            ok = run_permissions_wizard(headless=True)
            return 0 if ok is not False else 1
        else:
            if not silent:
                print("Starte den V4 Upgrade & Update Prozess via installer_main.py...\n")
                sys.stdout.flush()
            from Installer.update import start_installation_or_update
            result = start_installation_or_update(
                allow_first_install=False,
                headless=True,
            )
            return 0 if result is not False else 1
    except Exception as e:
        print(f"Fehler beim Ausführen von self_update.py: {e}")
        if not silent:
            print("\nBitte starte den Installer manuell:")
            print("  python3 installer_main.py")
        return 1

if __name__ == "__main__":
    sys.exit(main())
