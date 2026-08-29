#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Eng begrenzter Abschluss eines bereits laufenden v5.4.4/v5.4.4a-Updates.

Dieser Pfad startet ausdrücklich keine zweite Update-Transaktion. Er übernimmt
nur einen vom veröffentlichten Parent bereits vollständig angelegten
``e3dc_update_safety_v1``-Vertrag: Vollbackup, Paket-Preimage, Aktorruhe,
Bootblock und globaler Lock bleiben bis zur atomaren Commit-Grenze Eigentum des
alten Parents. Bei jedem Fehler vor dieser Grenze bleibt das Receipt ``pending``
und der wartende Parent führt seinen eigenen, bereits gebundenen Rückweg aus.

Der Einstieg ist nicht öffentlich. ``release_finalize.py`` erreicht ihn nur
nach bytegenauer Erkennung des alten Wrapper- und Finalizer-ARGV-Vertrags.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import replace
from pathlib import Path

from . import update_legacy_safety as legacy_codec


def _same_receipt_shape(
    first: legacy_codec.LegacyUpdateSafetyReceipt,
    second: legacy_codec.LegacyUpdateSafetyReceipt,
) -> bool:
    ignored = {"state", "receipt_sha256"}
    return all(
        getattr(first, name) == getattr(second, name)
        for name in legacy_codec.LegacyUpdateSafetyReceipt.__dataclass_fields__
        if name not in ignored
    )


def _legacy_record(
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    state: str,
) -> dict:
    if state not in {"pending", "committed"}:
        raise RuntimeError("Legacy-Forward-Receiptzustand ist ungültig")
    return {
        "schema": legacy_codec.LEGACY_UPDATE_SAFETY_SCHEMA,
        "state": state,
        "transaction_id": receipt.transaction_id,
        "target": {
            "commit": receipt.target_commit,
            "tag": receipt.target_tag,
            "role": receipt.role,
        },
        "backup": {
            "dir": receipt.backup_dir,
            "dev": receipt.backup_dev,
            "ino": receipt.backup_ino,
            "id": receipt.backup_id,
            "manifest_sha256": receipt.backup_manifest_sha256,
        },
        "bootblock": {
            "units": list(receipt.units),
            "created_directories": list(receipt.created_directories),
            "dropin_payload_sha256": receipt.dropin_payload_sha256,
            "dropin_identities": [list(item) for item in receipt.dropin_identities],
        },
        "finalizer": {
            "unit": receipt.finalizer_unit,
            "runtime_directory": receipt.runtime_directory,
            "token_path": receipt.token_path,
        },
    }


def _canonical_receipt_bytes(record: dict) -> bytes:
    return (
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _assert_legacy_managed_finalizer_service(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    execution_root: str,
) -> None:
    """Bindet die unveränderte alte Unit an denselben vollständigen Snapshot."""

    active_runner = str(
        os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or ""
    )
    if (
        not execution_root
        or os.path.realpath(active_runner) != execution_root
    ):
        raise RuntimeError(
            "Legacy-Forward besitzt keine exakte Runnerbindung"
        )
    update_module._assert_managed_finalizer_service(receipt)
    if os.path.realpath(
        os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", "")
    ) != execution_root:
        raise RuntimeError("Legacy-Forward-Runnerumgebung driftete beim Unit-Readback")


def _rebind_pending_contract_surfaces(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
    repo_dir: str,
    execution_root: str,
    bind_source: bool,
) -> str | None:
    metadata = update_module._revalidate_bound_active_legacy_forward_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    if receipt.state != "pending" or receipt.receipt_sha256 != hashlib.sha256(
        _canonical_receipt_bytes(_legacy_record(receipt, state="pending"))
    ).hexdigest():
        raise RuntimeError("Aktives Legacy-Forward-Receipt ist nicht exakt pending")
    if (int(metadata.st_dev), int(metadata.st_ino)) != (
        int(receipt_dev),
        int(receipt_ino),
    ):
        raise RuntimeError("Aktives Legacy-Forward-Receipt driftete am Inode")
    update_module._assert_legacy_recovery_namespace_exclusive()
    update_module._rebind_legacy_update_safety_dropins(
        receipt,
        allow_missing=False,
    )
    update_module._verify_update_safety_marker(receipt, expected_present=True)
    update_module._reload_and_verify_update_safety_dropins(
        receipt,
        expected_present=True,
    )
    _assert_legacy_managed_finalizer_service(
        update_module,
        receipt,
        execution_root=execution_root,
    )
    if bind_source:
        return update_module._bind_active_legacy_forward_backup_source(
            receipt,
            requested_install_root=repo_dir,
        )
    return None


def _revalidate_pending_contract(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
    repo_dir: str,
    execution_root: str,
    bind_source: bool,
) -> str | None:
    """Bindet den v1-Vertrag, solange noch kein Produktdienst laufen darf."""

    source_tag = _rebind_pending_contract_surfaces(
        update_module,
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
        repo_dir=repo_dir,
        execution_root=execution_root,
        bind_source=bind_source,
    )
    update_module._assert_strict_update_writer_quiescence(
        repo_dir=repo_dir,
        transaction_id=receipt.transaction_id,
    )
    return source_tag


def _revalidate_post_start_pending_contract(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
    repo_dir: str,
    execution_root: str,
    start_token_identity: tuple[int, ...],
    restart_services,
    transition_state,
    projected_piguard: bool,
    apache_state: tuple[bool, bool, str],
) -> str:
    """Bindet den Pending-Vertrag nach dem verifizierten Produktdienststart.

    Die vollständige Writer-Ruhe ist nur vor dem Öffnen der Startlease
    zulässig. Nach dem erfolgreichen Gesundheitsgate müssen die Produktdienste
    laufen dürfen; weiterhin strikt gebunden bleiben aber Transaktionslock,
    Receipt, Marker, Drop-ins, Finalizer und Quellbackup. Eine zweite
    Update-/Finalizer-Kette bleibt unverändert verboten.
    """

    update_module._assert_no_concurrent_update_processes(
        receipt.transaction_id
    )
    update_module._required_update_lock_fd()
    source_tag = _rebind_pending_contract_surfaces(
        update_module,
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
        repo_dir=repo_dir,
        execution_root=execution_root,
        bind_source=True,
    )
    _revalidate_start_token(
        update_module,
        receipt,
        expected_identity=start_token_identity,
    )
    if not update_module._post_update_healthcheck(
        restart_services,
        transition_state=transition_state,
        projected_piguard=projected_piguard,
        check_web=False,
        check_http=False,
    ):
        raise RuntimeError(
            "Produktdienste drifteten unmittelbar vor dem Legacy-v1-Commit"
        )
    # Der Gesundheitstest bindet bereits Rolle und Konfiguration vor seinen
    # Dienstabfragen. Ein zweiter Readback danach schließt auch dieses kurze
    # Prüfintervall, ohne PID- oder cgroup-Details unnötig einzufrieren.
    update_module._verify_transition_state(transition_state)
    _verify_legacy_apache_precommit(
        update_module,
        apache_state,
        validate_config=True,
    )
    update_module._required_update_lock_fd()
    update_module._assert_no_concurrent_update_processes(
        receipt.transaction_id
    )
    _revalidate_start_token(
        update_module,
        receipt,
        expected_identity=start_token_identity,
    )
    metadata = update_module._revalidate_bound_active_legacy_forward_receipt(
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
    )
    if (int(metadata.st_dev), int(metadata.st_ino)) != (
        int(receipt_dev),
        int(receipt_ino),
    ):
        raise RuntimeError(
            "Aktives Legacy-Forward-Receipt driftete vor dem Post-Start-Commit"
        )
    if not source_tag:
        raise RuntimeError("Legacy-Forward-Quellbackup ist vor dem Commit ungebunden")
    return str(source_tag)


def _legacy_expected_dropins(update_module, receipt) -> dict[str, dict]:
    update_module._rebind_legacy_update_safety_dropins(
        receipt,
        allow_missing=False,
    )
    identities = {
        unit: (int(device), int(inode))
        for unit, device, inode in receipt.dropin_identities
    }
    payload = receipt.dropin_payload
    return {
        unit: {
            update_module._recovery_dropin_path(unit): {
                "bytes": payload,
                "dev": identities[unit][0],
                "ino": identities[unit][1],
                "uid": 0,
                "gid": 0,
                "mode": 0o644,
                "nlink": 1,
                "size": len(payload),
            }
        }
        for unit in receipt.units
    }


def _bind_legacy_apache_state(
    update_module,
    *,
    source_tag: str,
) -> tuple[bool, bool, str]:
    """Bindet Apache ohne die vom alten Parent nicht autorisierte Mutation.

    Der veröffentlichte v5.4.4-Parent hat eine vorhandene Apache-Unit nur bei
    aktivem Dienst als Recovery-Preimage akzeptiert. v5.4.4a hat zusätzlich
    einen absichtlich inaktiven Dienst gebunden. Beide Parent-Versionen hielten
    dieses Preimage ausschließlich in ihrem laufenden Prozess; das v1-Receipt
    enthält es nicht. Deshalb darf der Forward-Pfad den aktuellen Zustand nur
    frisch lesen und anschließend bis zum Receipt-Commit unverändert halten.
    """

    if source_tag not in legacy_codec.LEGACY_FORWARD_SOURCE_BLOBS:
        raise RuntimeError(
            "E3DC-UPD-LEGACY-APACHE-001: Apache besitzt keinen bekannten "
            "Legacy-v1-Parentvertrag. Lösung: Update abbrechen und den "
            "veröffentlichten Ziel-Updater erneut über den normalen Bootstrap starten."
        )
    state = tuple(update_module._capture_apache_service_prestate())
    if (
        len(state) != 3
        or not isinstance(state[0], bool)
        or not isinstance(state[1], bool)
        or not isinstance(state[2], str)
    ):
        raise RuntimeError(
            "E3DC-UPD-LEGACY-APACHE-002: Apache-Zustand ist nicht typisiert lesbar. "
            "Lösung: `sudo systemctl status --no-pager apache2.service` prüfen "
            "und nach einem stabilen Zustand denselben Updatebefehl erneut starten."
        )
    available, active, unit_state = state
    if source_tag == "v5.4.4" and (not available or not active):
        raise RuntimeError(
            "E3DC-UPD-LEGACY-APACHE-003: Apache ist nicht mehr vorhanden und "
            "aktiv und widerspricht dem gebundenen v5.4.4-Parentvertrag. "
            "Lösung: `sudo systemctl status --no-pager apache2.service` prüfen, "
            "den vorher aktiven Dienstzustand wiederherstellen und danach denselben "
            "Updatebefehl erneut starten."
        )
    return available, active, unit_state


def _verify_legacy_apache_precommit(
    update_module,
    expected: tuple[bool, bool, str],
    *,
    validate_config: bool,
) -> None:
    """Beweist den unveränderten Apache-Zustand vor der v1-Commit-Grenze."""

    current = tuple(update_module._capture_apache_service_prestate())
    if current != expected:
        raise RuntimeError(
            "E3DC-UPD-LEGACY-APACHE-004: Apache driftete während des Updates "
            f"(erwartet={expected}, aktuell={current}). Lösung: Apache-Zustand und "
            "Journal prüfen; nach dem automatisch bestätigten Parent-Rücklauf "
            "denselben Updatebefehl erneut starten."
        )
    available, _active, _unit_state = expected
    if validate_config and available:
        result = update_module._run_argv(
            ["sudo", "/usr/sbin/apache2ctl", "configtest"],
            timeout=30,
        )
        if (
            not result.get("success")
            or result.get("timed_out")
            or int(result.get("returncode", -1)) != 0
        ):
            detail = update_module._combined_process_diagnostics(
                result,
                maximum=800,
            )
            raise RuntimeError(
                "E3DC-UPD-LEGACY-APACHE-005: Apache-Konfiguration ist vor der "
                f"Commit-Grenze ungültig: {detail}. Lösung: `sudo "
                "/usr/sbin/apache2ctl configtest` ausführen, den dort genannten "
                "Konfigurationsfehler beheben und das Update erneut starten."
            )


def _complete_legacy_apache_after_commit(
    update_module,
    expected: tuple[bool, bool, str],
) -> None:
    """Lädt ausschließlich einen bereits aktiven Apache nach Commit neu.

    Ein vorher inaktiver oder abwesender Apache bleibt unverändert. Insbesondere
    gibt es in diesem Pfad absichtlich kein ``systemctl start``.
    """

    current = tuple(update_module._capture_apache_service_prestate())
    if current != expected:
        raise RuntimeError(
            "Apache driftete vor dem Legacy-v1-PostCommit-Reload "
            f"(erwartet={expected}, aktuell={current})"
        )
    available, active, _unit_state = expected
    if not available or not active:
        return
    configtest = update_module._run_argv(
        ["sudo", "/usr/sbin/apache2ctl", "configtest"],
        timeout=30,
    )
    if (
        not configtest.get("success")
        or configtest.get("timed_out")
        or int(configtest.get("returncode", -1)) != 0
    ):
        raise RuntimeError(
            "Apache-Konfiguration ist beim Legacy-v1-PostCommit-Abschluss ungültig: "
            + update_module._combined_process_diagnostics(configtest, maximum=800)
        )
    reloaded = update_module._run_argv(
        ["sudo", "systemctl", "reload", "apache2.service"],
        timeout=30,
    )
    if (
        not reloaded.get("success")
        or reloaded.get("timed_out")
        or int(reloaded.get("returncode", -1)) != 0
    ):
        raise RuntimeError(
            "Bereits aktiver Apache konnte nach Legacy-v1-Commit nicht neu geladen "
            "werden: "
            + update_module._combined_process_diagnostics(reloaded, maximum=800)
        )
    rebound = tuple(update_module._capture_apache_service_prestate())
    if rebound != expected:
        raise RuntimeError(
            "Apache-Zustand driftete durch den Legacy-v1-PostCommit-Reload "
            f"(erwartet={expected}, aktuell={rebound})"
        )
    http_errors = update_module._local_http_healthcheck()
    if http_errors:
        raise RuntimeError("; ".join(http_errors[:4]))


def _revalidate_start_token(
    update_module,
    receipt,
    *,
    expected_identity: tuple[int, ...],
) -> None:
    runtime_path = Path("/run") / receipt.runtime_directory
    if Path(receipt.token_path) != runtime_path / update_module.UPDATE_FINALIZER_TOKEN_NAME:
        raise RuntimeError("Legacy-Forward-Starttokenpfad driftete")
    runtime_descriptor = update_module._open_directory_nofollow(runtime_path)
    try:
        update_module._require_root_controlled_directory(
            runtime_descriptor,
            str(runtime_path),
            0o700,
        )
        payload = (
            f"E3DC_UPDATE_START_LEASE_V1:{receipt.transaction_id}\n"
        ).encode("ascii")
        metadata = update_module._read_exact_root_file_at(
            runtime_descriptor,
            update_module.UPDATE_FINALIZER_TOKEN_NAME,
            payload,
            0o600,
        )
        if tuple(update_module._file_identity(metadata)) != tuple(expected_identity):
            raise RuntimeError("Legacy-Forward-Starttoken driftete vor dem Commit")
    finally:
        os.close(runtime_descriptor)


def _create_start_token(
    update_module,
    receipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
    repo_dir: str,
    execution_root: str,
) -> tuple[int, ...]:
    _revalidate_pending_contract(
        update_module,
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
        repo_dir=repo_dir,
        execution_root=execution_root,
        bind_source=False,
    )
    runtime_path = Path("/run") / receipt.runtime_directory
    runtime_descriptor = update_module._open_directory_nofollow(runtime_path)
    try:
        update_module._require_root_controlled_directory(
            runtime_descriptor,
            str(runtime_path),
            0o700,
        )
        payload = (
            f"E3DC_UPDATE_START_LEASE_V1:{receipt.transaction_id}\n"
        ).encode("ascii")
        update_module._create_owned_exact_root_file_at(
            runtime_descriptor,
            update_module.UPDATE_FINALIZER_TOKEN_NAME,
            payload,
            0o600,
        )
        os.fsync(runtime_descriptor)
        token_metadata = update_module._read_exact_root_file_at(
            runtime_descriptor,
            update_module.UPDATE_FINALIZER_TOKEN_NAME,
            payload,
            0o600,
        )
        return tuple(update_module._file_identity(token_metadata))
    finally:
        os.close(runtime_descriptor)


def _commit_receipt(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
    repo_dir: str,
    execution_root: str,
    start_token_identity: tuple[int, ...],
    restart_services,
    transition_state,
    projected_piguard: bool,
    apache_state: tuple[bool, bool, str],
) -> tuple[legacy_codec.LegacyUpdateSafetyReceipt, int, int]:
    # Nach dem verifizierten Dienststart dürfen Produkt-Writer laufen. Die
    # Transaktions-, Receipt- und Backup-Identität wird dennoch unmittelbar
    # vor der einzigen irreversiblen Namensmutation erneut vollständig
    # gebunden.
    _revalidate_post_start_pending_contract(
        update_module,
        receipt,
        receipt_dev=receipt_dev,
        receipt_ino=receipt_ino,
        repo_dir=repo_dir,
        execution_root=execution_root,
        start_token_identity=start_token_identity,
        restart_services=restart_services,
        transition_state=transition_state,
        projected_piguard=projected_piguard,
        apache_state=apache_state,
    )
    payload = _canonical_receipt_bytes(_legacy_record(receipt, state="committed"))
    state_descriptor = update_module._open_recovery_bootblock_state_directory()
    temporary_name = (
        f".e3dc-legacy-forward-receipt-{os.getpid()}-{secrets.token_hex(12)}"
    )
    descriptor = None
    replaced = False
    try:
        current = update_module._read_bound_root_file_at(
            state_descriptor,
            update_module.UPDATE_SAFETY_RECEIPT_NAME,
            maximum=legacy_codec.LEGACY_UPDATE_SAFETY_MAX_BYTES,
            mode=0o600,
        )
        if current is None or (
            int(current[1].st_dev),
            int(current[1].st_ino),
        ) != (int(receipt_dev), int(receipt_ino)):
            raise RuntimeError("Legacy-Forward-Receipt driftete vor dem Commit")
        rebound = legacy_codec.parse_active_legacy_forward_receipt(
            current[0],
            expected_target_commit=receipt.target_commit,
            expected_target_tag=receipt.target_tag,
        )
        if rebound != receipt:
            raise RuntimeError("Legacy-Forward-Receiptbytes drifteten vor dem Commit")
        descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=state_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("Committed Legacy-Forward-Receipt blieb unvollständig")
            view = view[written:]
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or staged.st_nlink != 1
            or staged.st_uid != 0
            or staged.st_gid != 0
            or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_size != len(payload)
            or update_module._repo_descriptor_has_unsafe_xattrs(descriptor)
        ):
            raise RuntimeError("Gestagtes Legacy-Forward-Receipt ist unsicher")
        os.replace(
            temporary_name,
            update_module.UPDATE_SAFETY_RECEIPT_NAME,
            src_dir_fd=state_descriptor,
            dst_dir_fd=state_descriptor,
        )
        replaced = True
        os.fsync(state_descriptor)
        current = update_module._read_bound_root_file_at(
            state_descriptor,
            update_module.UPDATE_SAFETY_RECEIPT_NAME,
            maximum=legacy_codec.LEGACY_UPDATE_SAFETY_MAX_BYTES,
            mode=0o600,
        )
        if current is None or current[0] != payload:
            raise RuntimeError("Committed Legacy-Forward-Receipt ist nicht lesbar")
        committed = legacy_codec.parse_active_legacy_forward_receipt(
            current[0],
            expected_target_commit=receipt.target_commit,
            expected_target_tag=receipt.target_tag,
        )
        if committed.state != "committed" or not _same_receipt_shape(
            committed,
            receipt,
        ):
            raise RuntimeError("Committed Legacy-Forward-Receipt änderte den Vertrag")
        return committed, int(current[1].st_dev), int(current[1].st_ino)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=state_descriptor)
            except FileNotFoundError:
                pass
        os.close(state_descriptor)


def _finish_committed_gate(
    update_module,
    receipt: legacy_codec.LegacyUpdateSafetyReceipt,
    *,
    receipt_dev: int,
    receipt_ino: int,
) -> None:
    current, metadata = update_module._read_bound_active_legacy_forward_receipt(
        expected_target_commit=receipt.target_commit,
        expected_target_tag=receipt.target_tag,
    )
    if (
        current != receipt
        or current.state != "committed"
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(receipt_dev), int(receipt_ino))
    ):
        raise RuntimeError("Committed Legacy-Forward-Receipt driftete")
    update_module._assert_legacy_recovery_namespace_exclusive()
    update_module._rebind_legacy_update_safety_dropins(
        receipt,
        allow_missing=False,
    )
    marker_name = Path(update_module.RECOVERY_BOOTBLOCK_MARKER).name
    marker_payload = update_module._recovery_bootblock_marker_payload(
        receipt.transaction_id
    )
    state_descriptor = update_module._open_recovery_bootblock_state_directory()
    try:
        marker = update_module._read_exact_root_file_at(
            state_descriptor,
            marker_name,
            marker_payload,
            0o600,
        )
        if marker is None:
            raise RuntimeError("Legacy-Forward-Marker fehlt vor dem Commit-Cleanup")
        marker_identity = update_module._file_identity(marker)
        current, metadata = (
            update_module._read_bound_active_legacy_forward_receipt(
                expected_target_commit=receipt.target_commit,
                expected_target_tag=receipt.target_tag,
            )
        )
        if (
            current != receipt
            or current.state != "committed"
            or (int(metadata.st_dev), int(metadata.st_ino))
            != (int(receipt_dev), int(receipt_ino))
        ):
            raise RuntimeError(
                "Committed Legacy-Forward-Receipt driftete vor dem Marker-Unlink"
            )
        # Der Receipt-Rebind darf selbst kein Marker-Austauschfenster öffnen.
        # Deshalb werden Bytes und Inode des Markers erst danach erneut direkt
        # am gebundenen Verzeichnis geprüft.
        rebound = update_module._read_exact_root_file_at(
            state_descriptor,
            marker_name,
            marker_payload,
            0o600,
        )
        named = os.stat(
            marker_name,
            dir_fd=state_descriptor,
            follow_symlinks=False,
        )
        if (
            rebound is None
            or update_module._file_identity(rebound) != marker_identity
            or update_module._file_identity(named) != marker_identity
        ):
            raise RuntimeError("Legacy-Forward-Marker driftete vor dem Unlink")
        os.unlink(marker_name, dir_fd=state_descriptor)
        os.fsync(state_descriptor)
    finally:
        os.close(state_descriptor)
    update_module._verify_update_safety_marker(receipt, expected_present=False)
    update_module._remove_owned_update_safety_dropins(
        units=receipt.units,
        identities={
            unit: (int(device), int(inode))
            for unit, device, inode in receipt.dropin_identities
        },
        created_directories=receipt.created_directories,
        payload=receipt.dropin_payload,
        allow_missing=False,
    )
    update_module._reload_and_verify_update_safety_dropins(
        receipt,
        expected_present=False,
    )
    rebound, rebound_metadata = (
        update_module._read_bound_active_legacy_forward_receipt(
            expected_target_commit=receipt.target_commit,
            expected_target_tag=receipt.target_tag,
        )
    )
    if (
        rebound != receipt
        or (int(rebound_metadata.st_dev), int(rebound_metadata.st_ino))
        != (int(receipt_dev), int(receipt_ino))
    ):
        raise RuntimeError("Committed Legacy-Forward-Receipt driftete nach Cleanup")


def finalize_active_legacy_v1_release(
    update_module,
    *,
    repo_dir: str,
    execution_root: str,
    target_commit: str,
    target_tag: str,
    expected_role: str,
    expected_config_state: str,
    expected_config_sha256: str,
    expected_units_sha256: str,
    expected_legacy_activity: str,
    expected_venv_state: str,
    expected_venv_path: str,
    update_safety_transaction: str,
    update_safety_receipt_sha256: str,
    update_safety_service_unit: str,
    update_safety_runtime_directory: str,
    update_safety_token_path: str,
    update_safety_receipt_device: int,
    update_safety_receipt_inode: int,
    explicit_download_bootstrap: bool,
    privileged_preimages,
) -> str:
    """Finalisiert exakt den bereits laufenden v1-Übergang des alten Parents."""

    target_root = update_module._validate_bootstrap_install_path(repo_dir)
    snapshot_root = update_module._validate_bootstrap_install_path(execution_root)
    loaded_root = os.path.dirname(update_module.INSTALLER_DIR)
    if (
        os.path.realpath(loaded_root) != snapshot_root
        or os.path.realpath(update_module.INSTALL_PATH) != target_root
        or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_ROOT", "")) != target_root
        or os.path.realpath(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT", ""))
        != snapshot_root
    ):
        raise RuntimeError(
            "Legacy-Forward-Finalizer wurde nicht aus dem versiegelten Ziel-Snapshot geladen"
        )
    commit = update_module._validate_full_commit(target_commit)
    tag = update_module._normalize_release_tag(target_tag)
    role = str(expected_role or "").strip().lower()
    if role not in update_module.VALID_HA_ROLES:
        raise RuntimeError("Legacy-Forward-Rolle ist ungültig")
    if not isinstance(explicit_download_bootstrap, bool):
        raise RuntimeError("Legacy-Forward-Bootstrapvertrag ist nicht boolesch")

    receipt, metadata = update_module._read_bound_active_legacy_forward_receipt(
        expected_target_commit=commit,
        expected_target_tag=tag,
    )
    if (
        receipt.state != "pending"
        or receipt.transaction_id != update_safety_transaction
        or receipt.receipt_sha256 != update_safety_receipt_sha256
        or receipt.finalizer_unit != update_safety_service_unit
        or receipt.runtime_directory != update_safety_runtime_directory
        or receipt.token_path != update_safety_token_path
        or receipt.role != role
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (int(update_safety_receipt_device), int(update_safety_receipt_inode))
    ):
        raise RuntimeError("Legacy-Forward-Receipt widerspricht dem Wrappervertrag")
    source_tag = _revalidate_pending_contract(
        update_module,
        receipt,
        receipt_dev=update_safety_receipt_device,
        receipt_ino=update_safety_receipt_inode,
        repo_dir=target_root,
        execution_root=snapshot_root,
        bind_source=True,
    )
    apache_state = _bind_legacy_apache_state(
        update_module,
        source_tag=str(source_tag),
    )

    update_module._announce_finalizer_phase(
        1,
        7,
        f"Legacy-Forward {source_tag} und Zielbindung prüfen",
    )
    actual_commit = update_module._resolve_git_commit(
        target_root,
        "HEAD",
        update_module.get_install_user(),
        **(
            update_module._root_git_call_kwargs(True)
            if explicit_download_bootstrap
            else {}
        ),
    )
    if not actual_commit or not update_module._exact_commit_matches(
        commit,
        actual_commit,
    ):
        raise RuntimeError("Legacy-Forward-Finalizer sieht nicht den Ziel-Commit")

    state = update_module._capture_transition_state(
        expected_role=role,
        allow_missing_config=expected_config_state == "missing",
    )
    update_module._verify_bound_target_state(
        state,
        expected_config_state=expected_config_state,
        expected_config_sha256=expected_config_sha256,
        expected_units_sha256=expected_units_sha256,
        expected_legacy_activity=expected_legacy_activity,
    )
    watchdog_runtime_required = update_module._watchdog_runtime_venv_required(state)
    install_user = update_module.get_install_user()
    web_program_files, web_program_directories = (
        update_module._web_program_contract_from_commit(
            target_root,
            commit,
            install_user,
            root_authority=explicit_download_bootstrap,
        )
    )
    policy = update_module._read_policy_from_commit(
        target_root,
        commit,
        install_user,
        **(
            update_module._root_git_call_kwargs(True)
            if explicit_download_bootstrap
            else {}
        ),
    )
    update_module._validate_local_target_release_binding(
        policy,
        target_root,
        commit,
        tag,
        install_user,
        **(
            update_module._root_git_call_kwargs(True)
            if explicit_download_bootstrap
            else {}
        ),
    )
    restart_services = update_module._validated_restart_services(policy, state)
    guarded_units = set(receipt.units)
    unguarded = sorted(
        {
            update_module._unit_name(service)
            for service in restart_services
        }
        - guarded_units
    )
    if unguarded:
        raise RuntimeError(
            "Zielrelease verlangt vom alten v1-Bootblock nicht geschützte Dienste: "
            + ", ".join(unguarded)
        )
    pip_packages = update_module._validated_venv_pip_packages(policy)
    if pip_packages:
        if expected_venv_state not in {"present", "missing"}:
            raise RuntimeError("Legacy-Forward-venv-Preimage ist ungültig")
        if not os.path.isabs(str(expected_venv_path or "")):
            raise RuntimeError("Legacy-Forward-venv-Pfad ist nicht absolut")
    elif watchdog_runtime_required:
        if expected_venv_state != "present" or not os.path.isabs(
            str(expected_venv_path or "")
        ):
            raise RuntimeError("Legacy-Forward-Watchdog-venv ist nicht gebunden")
    elif expected_venv_state != "unused" or expected_venv_path:
        raise RuntimeError("Legacy-Forward-venv-Preimage ist ohne Paketpolicy unzulässig")
    update_module._validate_watchdog_runtime_venv_contract(
        required=watchdog_runtime_required,
        expected_venv_state=expected_venv_state,
        expected_venv_path=expected_venv_path,
        target_root=target_root,
        install_user=install_user,
    )

    bootstrap_projection = None
    update_module._announce_finalizer_phase(
        2,
        7,
        "Paket- und Repositoryzustand im Parent-Preimage herstellen",
    )
    update_module._secure_repo_permissions(
        target_root,
        install_user,
        expected_commit=commit,
        **({"root_git_authority": True} if explicit_download_bootstrap else {}),
    )
    update_module._verify_worktree_policy(target_root, policy)
    update_module._migrate_bootstrap_legacy_config(target_root, state)
    update_module._apply_verified_package_policy(
        policy,
        install_user,
        expected_venv_state=expected_venv_state,
        expected_venv_path=expected_venv_path,
    )
    if explicit_download_bootstrap:
        projection_config, projection_sha256 = state.config, state.config_sha256
        if state.bootstrap_legacy_config:
            projection_config, migrated_raw = update_module._read_json_nofollow(
                state.config_path
            )
            if (
                str(projection_config.get("ha_mode") or "").strip().lower()
                != state.ha_role
            ):
                raise RuntimeError("Legacy-Webprojektion verlor die gebundene Rolle")
            projection_sha256 = hashlib.sha256(migrated_raw).hexdigest()
        bootstrap_projection = update_module.project_download_bootstrap_metadata(
            target_root,
            install_user,
            venv_path=expected_venv_path,
            expected_v4_config=projection_config,
            expected_v4_sha256=projection_sha256,
        )
        state = replace(
            state,
            config=dict(bootstrap_projection["config"]),
            config_sha256=str(bootstrap_projection["config_sha256"]),
        )
        expected_config_sha256 = state.config_sha256

    delete_ok, delete_errors = update_module._delete_approved_stale_paths(
        policy.get("delete_files") or []
    )
    if not delete_ok:
        raise RuntimeError(
            "Stale-Delete-Positivliste verletzt: " + "; ".join(delete_errors)
        )
    _revalidate_pending_contract(
        update_module,
        receipt,
        receipt_dev=update_safety_receipt_device,
        receipt_ino=update_safety_receipt_inode,
        repo_dir=target_root,
        execution_root=snapshot_root,
        bind_source=False,
    )

    update_module._announce_finalizer_phase(
        3,
        7,
        "Webroot und Berechtigungen synchronisieren",
    )
    update_module._sync_release_web(
        target_root,
        policy,
        allow_config_bootstrap=state.bootstrap_legacy_config,
        program_files=web_program_files,
        program_directories=web_program_directories,
    )
    if bootstrap_projection is not None and state.bootstrap_legacy_config:
        legacy_config, legacy_raw = update_module._read_json_nofollow(
            state.config_path
        )
        if (
            any(
                legacy_config.get(key) != value
                for key, value in state.config.items()
            )
            or set(legacy_config)
            - set(state.config)
            - set(update_module.WEB_CONFIG_START_DEFAULTS)
        ):
            raise RuntimeError("Legacy-Webprojektion veränderte gebundene Fachwerte")
        state = replace(
            state,
            config=legacy_config,
            config_sha256=hashlib.sha256(legacy_raw).hexdigest(),
        )
        expected_config_sha256 = state.config_sha256
    if policy.get("run_permissions", True):
        from .permissions import run_permissions_wizard

        expected_commit_env = "E3DC_RELEASE_EXPECTED_COMMIT"
        previous_expected_commit = os.environ.get(expected_commit_env)
        os.environ[expected_commit_env] = commit
        try:
            if run_permissions_wizard(
                headless=True,
                release_quiesced=True,
                bound_privileged_preimages=privileged_preimages,
                program_files=web_program_files,
                program_directories=web_program_directories,
            ) is False:
                raise RuntimeError("Berechtigungsreparatur fehlgeschlagen")
        finally:
            if previous_expected_commit is None:
                os.environ.pop(expected_commit_env, None)
            else:
                os.environ[expected_commit_env] = previous_expected_commit
        update_module._secure_repo_permissions(
            target_root,
            install_user,
            expected_commit=commit,
        )
    _revalidate_pending_contract(
        update_module,
        receipt,
        receipt_dev=update_safety_receipt_device,
        receipt_ino=update_safety_receipt_inode,
        repo_dir=target_root,
        execution_root=snapshot_root,
        bind_source=False,
    )

    from .permissions import (
        PI_GUARD_PATH,
        ensure_private_ml_model_store,
        harden_web_program_permissions,
        refresh_watchdog_guard_script,
        storage_manager_writer_contract,
    )
    from .ramdisk_guard import require_ramdisk_service_dropins

    if not ensure_private_ml_model_store():
        raise RuntimeError("Privater ML-Modellspeicher konnte nicht vorbereitet werden")
    if not harden_web_program_permissions(
        program_files=web_program_files,
        program_directories=web_program_directories,
    ):
        raise RuntimeError("Web-Programmrechte konnten nicht gehärtet werden")
    update_module._validate_live_install_context_as_web(target_root)
    update_module._announce_finalizer_phase(
        4,
        7,
        "Kernservices und Migrationen unter v1-Bootblock vorbereiten",
    )
    expected_service_dropins = _legacy_expected_dropins(update_module, receipt)
    if not update_module.migrate_storage_manager_next_override(
        allow_redundant_current_override=explicit_download_bootstrap,
    ):
        raise RuntimeError("Storage-Service-Migration ist fehlgeschlagen")
    if not update_module._ensure_install_center_core_services(
        expected_recovery_dropins=expected_service_dropins,
        allow_optional_not_found_compat=explicit_download_bootstrap,
    ):
        raise RuntimeError("Kernservice-Installation ist unvollständig")
    storage_writer = storage_manager_writer_contract()
    if not storage_writer.get("ok"):
        raise RuntimeError(
            "Storage-Single-Writer-Vertrag ist verletzt: "
            + ", ".join(storage_writer.get("blockers") or ["unbekannt"])
        )
    ramdisk_dropins = require_ramdisk_service_dropins()
    if ramdisk_dropins.get("skipped"):
        raise RuntimeError("Bare-Metal-Update übersprang tmpfs-Startsperren")

    def refresh_bound_watchdog(*, start_service=True):
        if not watchdog_runtime_required:
            return refresh_watchdog_guard_script(start_service=start_service)
        return update_module._run_with_bound_bootstrap_venv(
            expected_venv_path,
            lambda: refresh_watchdog_guard_script(start_service=start_service),
        )

    watchdog_refresh_required = bool(
        os.path.exists(PI_GUARD_PATH) or update_module._service_unit_exists("piguard")
    )
    if watchdog_refresh_required != watchdog_runtime_required:
        raise RuntimeError("Watchdog-Bestand driftete vor der Guard-Projektion")
    if not refresh_bound_watchdog(start_service=False):
        raise RuntimeError("Watchdog-Guard konnte unter v1-Bootblock nicht aktualisiert werden")
    projected_piguard = watchdog_refresh_required
    if projected_piguard and not update_module._service_unit_exists("piguard"):
        raise RuntimeError("Quiesced Watchdog-Projektion besitzt keine geladene Unit")
    update_module._project_bare_metal_logrotate_config(
        repo_dir=target_root,
        target_commit=commit,
        install_user=install_user,
    )
    if not update_module._prepare_v4_service_activation(
        services=restart_services,
        transition_state=state,
        projected_piguard=projected_piguard,
    ):
        raise RuntimeError("Persistente Dienstvorbereitung blieb unvollständig")
    pause_reason = f"update-safety:{receipt.transaction_id}"
    update_module._enable_watchdog_update_pause(pause_reason)
    update_module._verify_watchdog_pause_fresh(pause_reason)
    # Das v1-Startgate darf erst öffnen, nachdem auch der vom alten Parent
    # eingefrorene Rollen-, Konfigurations- und Unitzustand unmittelbar vor
    # dem ersten Dienststart erneut bestätigt wurde. Die anschließende
    # Tokenfunktion bindet zusätzlich Receipt, Marker, Drop-ins, Servicelease
    # und Writer-Ruhe in demselben Zyklus.
    update_module._verify_transition_state(state)
    start_token_identity = _create_start_token(
        update_module,
        receipt,
        receipt_dev=update_safety_receipt_device,
        receipt_ino=update_safety_receipt_inode,
        repo_dir=target_root,
        execution_root=snapshot_root,
    )

    update_module._announce_finalizer_phase(
        5,
        7,
        "Dienste aktivieren und geordnet starten",
    )
    if not update_module._restart_v4_services(
        headless=True,
        services=restart_services,
        transition_state=state,
        prepared_start_only=True,
        projected_piguard=projected_piguard,
    ):
        raise RuntimeError("Erwartete Dienste konnten nicht vollständig gestartet werden")
    update_module._announce_finalizer_phase(
        6,
        7,
        "Gesundheit und Bootvertrag verifizieren",
    )
    _verify_legacy_apache_precommit(
        update_module,
        apache_state,
        validate_config=True,
    )
    if not update_module._post_update_healthcheck(
        restart_services,
        transition_state=state,
        projected_piguard=projected_piguard,
        check_http=apache_state[1],
    ):
        update_module._stop_v4_services(restart_services)
        raise RuntimeError("Dienst-/HTTP-/HA-Gesundheitsgate fehlgeschlagen")
    try:
        from .boot_sanity import check_boot_sanity

        boot_ok = check_boot_sanity(verbose=True)
    except Exception as exc:
        raise RuntimeError(
            f"Boot-Sanitycheck konnte nicht ausgeführt werden: {exc}"
        ) from exc
    if not boot_ok:
        update_module._stop_v4_services(restart_services)
        raise RuntimeError("Boot-Sanity-Gate fehlgeschlagen")
    if bootstrap_projection is not None:
        update_module._verify_projected_bootstrap_metadata_without_env(
            bootstrap_projection
        )
    update_module._verify_transition_state(state)
    if expected_config_state == "present" or bootstrap_projection is not None:
        _config, raw = update_module._read_json_nofollow(state.config_path)
        if hashlib.sha256(raw).hexdigest() != expected_config_sha256:
            raise RuntimeError("Betriebskonfiguration driftete im Legacy-Forward")
    current_head = update_module._resolve_git_commit(
        target_root,
        "HEAD",
        install_user,
    )
    if not current_head or not update_module._exact_commit_matches(
        commit,
        current_head,
    ):
        raise RuntimeError("Repository-HEAD driftete im Legacy-Forward-Readback")
    _verify_legacy_apache_precommit(
        update_module,
        apache_state,
        validate_config=True,
    )

    committed = None
    committed_dev = -1
    committed_ino = -1
    try:
        committed, committed_dev, committed_ino = _commit_receipt(
            update_module,
            receipt,
            receipt_dev=update_safety_receipt_device,
            receipt_ino=update_safety_receipt_inode,
            repo_dir=target_root,
            execution_root=snapshot_root,
            start_token_identity=start_token_identity,
            restart_services=restart_services,
            transition_state=state,
            projected_piguard=projected_piguard,
            apache_state=apache_state,
        )
        try:
            _complete_legacy_apache_after_commit(
                update_module,
                apache_state,
            )
        except Exception as apache_exc:
            # Das v1-Receipt ist bereits durable committed; der veröffentlichte
            # Parent darf nun keinesfalls mehr in sein Altpreimage zurückrollen.
            # Apache blieb bis zu dieser Grenze unverändert und seine aktive
            # Instanz läuft weiter. Der reine PostCommit-Reload ist deshalb eine
            # klar sichtbare, aber nicht rollbackfähige Abschlusswarnung.
            update_module.update_logger.warning(
                "E3DC-UPD-LEGACY-APACHE-POSTCOMMIT-001: Bereits committed; "
                "Apache-Reload blieb offen. Lösung: `sudo /usr/sbin/apache2ctl "
                "configtest` prüfen und bei Syntax OK `sudo systemctl reload "
                "apache2.service` ausführen: %s",
                apache_exc,
            )
        _finish_committed_gate(
            update_module,
            committed,
            receipt_dev=committed_dev,
            receipt_ino=committed_ino,
        )
        update_module._announce_finalizer_phase(
            7,
            7,
            "Legacy-v1-Commit abschließen",
        )
    except BaseException as exc:
        if committed is None:
            raise
        raise update_module.UpdateSafetyPostCommitError(
            "Legacy-Forward brach nach atomarem committed v1-Receipt ab"
        ) from exc
    return str(source_tag)


__all__ = ("finalize_active_legacy_v1_release",)
