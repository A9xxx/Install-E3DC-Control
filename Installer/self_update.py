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

UPDATE_DISPATCHER = "/usr/local/sbin/e3dc-web-update-launcher"


def _dispatch_background_update() -> int:
    """Startet den einzigen regulären Bare-Metal-Update-Einstieg."""
    if os.geteuid() != 0:
        print("Der Update-Dispatcher benötigt Root-Rechte.", file=sys.stderr)
        print(f"Starte: sudo {UPDATE_DISPATCHER}", file=sys.stderr)
        return 77
    if not os.path.isfile(UPDATE_DISPATCHER) or not os.access(UPDATE_DISPATCHER, os.X_OK):
        print(
            f"Root-eigener Update-Dispatcher fehlt oder ist nicht ausführbar: "
            f"{UPDATE_DISPATCHER}",
            file=sys.stderr,
        )
        return 127
    print("Übergebe das Update an den root-eigenen Hintergrund-Dispatcher …")
    sys.stdout.flush()
    try:
        os.execv(UPDATE_DISPATCHER, [UPDATE_DISPATCHER])
    except OSError as exc:
        print(f"Update-Dispatcher konnte nicht gestartet werden: {exc}", file=sys.stderr)
        return 126
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    allowed = {"--silent", "--unattended", "--check", "--fix-permissions"}
    unknown = [argument for argument in arguments if argument not in allowed]
    if unknown or len(arguments) != len(set(arguments)):
        print(
            "Unzulässige oder doppelte self_update-Option: "
            + ", ".join(unknown or arguments),
            file=sys.stderr,
        )
        return 64
    silent = "--silent" in arguments
    unattended = "--unattended" in arguments
    is_check = "--check" in arguments
    is_fix_permissions = "--fix-permissions" in arguments
    if (is_check and is_fix_permissions) or (
        unattended and (is_check or is_fix_permissions)
    ):
        print("self_update-Aktionen dürfen nicht kombiniert werden.", file=sys.stderr)
        return 64
    if not is_check and not is_fix_permissions:
        return _dispatch_background_update()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    try:
        if is_check:
            from Installer.release_version import stable_update_check

            result = stable_update_check(os.path.abspath(repo_root))
            if not result.get("success"):
                print("Fehler bei der Update-Prüfung: " + str(result.get("error") or "unbekannt"))
                return 1
            if int(result.get("missing") or 0) == 0:
                print(f"System ist aktuell ({result.get('current_version')}).")
                return 0
            else:
                print(
                    f"Stable-Release {result.get('target_version')} ist verfügbar "
                    f"(installiert: {result.get('current_version')})."
                )
                return 0
        elif is_fix_permissions:
            from Installer.permissions import run_permissions_wizard
            ok = run_permissions_wizard(headless=True)
            return 0 if ok is not False else 1
    except Exception as e:
        print(f"Fehler beim Ausführen von self_update.py: {e}")
        if not silent:
            print("\nBitte starte den Installer manuell:")
            print("  python3 installer_main.py")
        return 1

if __name__ == "__main__":
    sys.exit(main())
