"""Verified backup restore and policy-bound stable release rollback."""

from __future__ import annotations

import os
import re
import subprocess
import sys

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from .backup import backup_current_version, choose_backup_version, restore_backup, restore_verified_backup
from .core import register_command
from .installer_config import get_install_path, get_install_user
from .logging_manager import get_or_create_logger, log_error, log_task_completed, log_warning

INSTALL_PATH = get_install_path()
rollback_logger = get_or_create_logger("rollback")


def is_valid_commit_hash(commit_hash: str) -> bool:
    """Validation helper only; arbitrary commit rollback is intentionally disabled."""
    return re.fullmatch(r"[0-9a-fA-F]{40}", str(commit_hash or "")) is not None


def git_commit_exists(repo_dir: str, commit_hash: str) -> bool:
    if not is_valid_commit_hash(commit_hash):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "cat-file", "-t", commit_hash],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_warning("rollback", f"Commit-Pruefung fehlgeschlagen: {exc}")
        return False
    return result.returncode == 0 and result.stdout.strip() == "commit"


def hard_stop_e3dc() -> bool:
    """Delegate to the complete catalog writer stop; legacy e3dc is never restarted."""
    from .update import V4_SERVICES, _stop_v4_services

    return _stop_v4_services(V4_SERVICES)


def start_e3dc(transition_state=None, services=None) -> bool:
    """Restart only role-/feature-expected catalog services and prove health."""
    from .update import (
        _capture_transition_state,
        _post_update_healthcheck,
        _restart_v4_services,
    )

    state = transition_state or _capture_transition_state()
    requested = list(services or [
        unit.removesuffix(".service")
        for unit in state.preinstalled_units
        if unit != "e3dc.service"
    ])
    if not _restart_v4_services(services=requested, transition_state=state):
        return False
    if not _post_update_healthcheck(requested, transition_state=state):
        hard_stop_e3dc()
        return False
    return True


def _boot_gate() -> bool:
    try:
        from .boot_sanity import check_boot_sanity

        return bool(check_boot_sanity(verbose=True))
    except Exception as exc:
        rollback_logger.error(f"Boot-Sanitycheck fehlgeschlagen: {exc}")
        return False


def _bind_current_restore_helpers():
    """Bindet aktuelle Migrations- und Gate-Funktionen vor dem Datei-Restore."""
    from .permissions import run_permissions_wizard
    from .update import (
        _run_argv,
        _verify_transition_state,
        migrate_storage_manager_next_override,
    )

    return {
        "migrate_storage_override": (
            lambda migrate=migrate_storage_manager_next_override:
            migrate(reload_systemd=False)
        ),
        "run_argv": _run_argv,
        "run_permissions": run_permissions_wizard,
        "verify_transition_state": _verify_transition_state,
    }


def _post_restore_storage_writer_gate(helpers) -> bool:
    """Migriert restaurierte Storage-Overrides vor dem strikten Writer-Gate."""
    try:
        if helpers["migrate_storage_override"]() is not True:
            rollback_logger.error(
                "Storage-Override-Migration nach Restore ist fehlgeschlagen."
            )
            return False
        reload_result = helpers["run_argv"](
            ["sudo", "systemctl", "daemon-reload"],
            timeout=20,
        )
        if not isinstance(reload_result, dict) or not reload_result.get("success"):
            rollback_logger.error(
                "systemd daemon-reload nach Storage-Override-Migration ist fehlgeschlagen."
            )
            return False
        if helpers["run_permissions"](headless=True) is not True:
            rollback_logger.error(
                "Storage-Single-Writer-/Berechtigungs-Gate nach Restore ist fehlgeschlagen."
            )
            return False
        return True
    except Exception as exc:
        rollback_logger.error(
            f"Storage-Restore-Gate konnte nicht sicher abgeschlossen werden: {exc}"
        )
        return False


def _restore_safety_backup(safety_backup: str, state, *, restore_helpers=None) -> bool:
    """Best-effort automatic reversal of a failed selected-backup restore."""
    try:
        helpers = restore_helpers or _bind_current_restore_helpers()
        hard_stop_e3dc()
        restore_verified_backup(safety_backup, install_path=INSTALL_PATH)
        if not _post_restore_storage_writer_gate(helpers):
            return False
        # Das verifizierte Sicherheitsbackup besitzt bereits seinen eigenen
        # Datei-, Owner- und Modusvertrag. Der aktuelle Git-Index kann zu
        # diesem älteren Backup gehören und darf ihn nicht neu interpretieren.
        helpers["verify_transition_state"](state)
        return start_e3dc(state) and _boot_gate()
    except Exception as exc:
        rollback_logger.error(f"Sicherheitsbackup konnte nicht wiederhergestellt werden: {exc}")
        return False


def rollback(backup_dir, *, confirmed: bool = False) -> bool:
    """Restore one verified manifest backup with full safety rollback and hard gates."""
    if os.path.exists("/.dockerenv"):
        print("[!] Datei-Rollback ist im Container nicht vorgesehen; nutze ein verifiziertes Image-Digest.")
        return False
    if not confirmed:
        answer = input(f"Backup {os.path.basename(backup_dir)} wirklich wiederherstellen? (ja/nein): ").strip().lower()
        if answer != "ja":
            print("[i] Rollback abgebrochen.")
            return True

    try:
        from .update import _capture_transition_state

        restore_helpers = _bind_current_restore_helpers()
        state = _capture_transition_state()
    except Exception as exc:
        print(f"[!] HA-/Shadow-Preflight fehlgeschlagen: {exc}")
        return False

    print("[->] Erstelle verifiziertes Sicherheits-Backup...")
    try:
        # The selected backup is protected from retention while the safety
        # backup is created in the same dedicated backup collection.
        safety_backup = backup_current_version(
            install_path=INSTALL_PATH,
            preserve_backup_paths=[backup_dir],
        )
    except Exception as exc:
        safety_backup = None
        rollback_logger.error(f"Sicherheits-Backup fehlgeschlagen: {exc}")
    if not safety_backup:
        print("[!] Sicherheits-Backup fehlt; Rollback abgebrochen.")
        return False
    if not hard_stop_e3dc():
        print("[!] Sichere Aktorruhe konnte nicht nachgewiesen werden.")
        return False

    try:
        if not restore_backup(backup_dir, install_path=INSTALL_PATH, confirmed=True):
            raise RuntimeError("Manifest-Restore fehlgeschlagen")
        # restore_backup hat Bytes und Metadaten bereits gegen das ausgewählte
        # Manifest gebunden. Ein möglicherweise neuerer Git-Index ist für
        # diesen expliziten Datei-Rückfall keine Autorität.
        if not _post_restore_storage_writer_gate(restore_helpers):
            raise RuntimeError("Storage-Migrations-/Berechtigungs-Gate fehlgeschlagen")
        restore_helpers["verify_transition_state"](state)
        if not start_e3dc(state):
            raise RuntimeError("Dienst-/HTTP-/HA-Gate fehlgeschlagen")
        if not _boot_gate():
            hard_stop_e3dc()
            raise RuntimeError("Boot-Sanity-Gate fehlgeschlagen")
    except Exception as exc:
        print(f"[!] Backup-Rollback fehlgeschlagen: {exc}")
        rollback_logger.error(f"Backup-Rollback fehlgeschlagen: {exc}")
        if _restore_safety_backup(
            safety_backup,
            state,
            restore_helpers=restore_helpers,
        ):
            print("[OK] Ausgangszustand aus Sicherheits-Backup wiederhergestellt.")
        else:
            print("[!] Ausgangszustand nicht beweisbar; Writer bleiben gestoppt.")
        return False

    print("[OK] Verifizierter Backup-Rollback abgeschlossen.")
    log_task_completed("Rollback (Backup)", details=os.path.basename(backup_dir))
    return True


def get_last_commits(limit=20):
    """Compatibility name: return only policy-authorized stable release tags."""
    del limit
    from .update import _rollback_release_map

    try:
        mapping = _rollback_release_map(INSTALL_PATH)
    except Exception as exc:
        rollback_logger.error(f"Rollback-Policy konnte nicht gelesen werden: {exc}")
        return None
    return [(sha, tag) for tag, sha in mapping.items()] or None


def choose_commit():
    """Choose a SHA-bound release tag; raw commit selection no longer exists."""
    releases = get_last_commits()
    if not releases:
        print("[i] Keine freigegebene Rueckfallversion vorhanden.")
        return None
    print("\n=== Freigegebene Stable-Rueckfallversionen ===\n")
    for index, (sha, tag) in enumerate(releases, start=1):
        print(f"  {index:2d}: {tag} ({sha[:12]})")
    choice = input("Welche Rueckfallversion installieren? (0=Abbrechen): ").strip()
    if not choice.isdigit() or int(choice) < 0 or int(choice) > len(releases):
        print("[!] Ungueltige Auswahl.")
        return None
    if int(choice) == 0:
        return None
    sha, tag = releases[int(choice) - 1]
    return tag, sha


def rollback_to_release_tag(tag: str, expected_sha: str | None = None) -> bool:
    """Use the same backup, websync, migration, boot and health path as update."""
    from .update import update_e3dc

    return bool(update_e3dc(
        headless=True,
        target_ref=tag,
        expected_release_sha=expected_sha,
    ))


def rollback_to_commit(commit_hash: str):
    """Arbitrary commits are forbidden; callers must select a policy release tag."""
    del commit_hash
    print("[!] Freie Commit-Rollbacks sind deaktiviert; waehle eine freigegebene Stable-Version.")
    return False


def rollback_to_commit_hash():
    print("[!] Freie Commit-Hashes sind deaktiviert; nutze das Stable-Release-Menue.")
    return False


def rollback_menu():
    backup_dir = choose_backup_version()
    if backup_dir:
        rollback(backup_dir)


def rollback_commit_menu():
    selected = choose_commit()
    if selected:
        tag, sha = selected
        answer = input(f"Rueckfall auf {tag} ({sha[:12]})? (j/n): ").strip().lower()
        if answer == "j":
            rollback_to_release_tag(tag, sha)


if not os.path.exists("/.dockerenv"):
    register_command("14", "Rollback (Datei-Backup)", rollback_menu, sort_order=14)
    register_command("12", "Rollback auf freigegebenes Stable-Release", rollback_commit_menu, sort_order=12)
