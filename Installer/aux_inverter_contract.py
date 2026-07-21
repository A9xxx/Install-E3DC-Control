#!/usr/bin/env python3
"""Neutraler Zusatz-WR-Vertrag mit enger, hashgebundener Einmalmigration.

Das Modul führt keine Hardware-I/O aus. Historische Eingänge werden nur über
neun fest gebundene SHA-256-Werte erkannt, vollständig validiert, atomar in
den kanonischen Vertrag überführt und anschließend nicht weiter gespiegelt.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


CANONICAL_PREFIX = "direct_marketing_aux_inverter_shelly_"
CONTRACT_SUFFIXES = (
    "override",
    "ip",
    "invert",
    "dynamic_unblock_enable",
    "unblock_threshold_w",
)
CONTRACT_STATUS_KEY = CANONICAL_PREFIX + "contract_status"
CONTRACT_REASON_KEY = CANONICAL_PREFIX + "contract_reason"
CONTRACT_SCHEMA = "direct_marketing_aux_inverter_config_v2"
STATE_MIGRATION_SCHEMA = "direct_marketing_aux_inverter_state_migration_v2"
MIN_SWITCH_INTERVAL_S = 600.0

# Exakt fünf historische Configkeys. Die Klartextnamen gehören nicht in den
# Public-Root und werden weder rekonstruiert noch als Prefix behandelt.
LEGACY_CONFIG_KEY_HASHES: Mapping[str, str] = {
    "9a0b518bae76f7b37d44c97c2fdeaaaede11b807df52ec44dfea6dca3dec891f": "override",
    "25563e9ef9d08a487df17f2dd5f606c8d8aae96bb3a183912e0587bec07888fb": "ip",
    "1bce79869b1e829646125410f0a5c70134fd913bb126cee6a16cc409050f8999": "invert",
    "781090dadc2db5edec7c769a46886808d49d659bb5130ed7ecc51b90a6e9fd20": "dynamic_unblock_enable",
    "b9c05075ad27bd8449658956d38be772e993a9b197fc0fb0d4a044f936b53a83": "unblock_threshold_w",
}

# Exakt drei historische Runtime-Basenamen und ein historischer JSON-Key.
LEGACY_RUNTIME_BASENAME_HASHES: Mapping[str, str] = {
    "a7c600bea9395f1ba5e4f2ec278623f9d7306ed2fad962fdde94f5eb7b7635b9": "state",
    "7a3c3f94a4e631144947561a7aefc342ec5c54d833957a4265544e1df0adb59d": "manual_lock",
    "ffd1d70ef78efe91af5edd7bdfa01c3d90c174c752839a49277213deaad4ac50": "guard",
}
LEGACY_JSON_KEY_HASH = "1b4bbeb23ee1498e7b7a7162025419072c567c3dca3767dca77b56bcb3d4504f"
CANONICAL_JSON_KEY = "direct_marketing_aux_inverter_shelly"

SAFE_CONTRACT: Dict[str, Any] = {
    "override": "local",
    "ip": "",
    "invert": 0,
    "dynamic_unblock_enable": 0,
    "unblock_threshold_w": 3000,
}


def _name_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file_hash(path: Path) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("migration source is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _decode_json_object_without_duplicates(raw: bytes) -> Dict[str, Any]:
    duplicates: list[str] = []

    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(str(key))
            result[key] = value
        return result

    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs_hook)
    if duplicates:
        raise ValueError("duplicate JSON keys are not allowed")
    if not isinstance(payload, dict):
        raise ValueError("JSON document must be an object")
    return payload


def _load_json_object_without_duplicates(path: Path) -> Tuple[Dict[str, Any], str]:
    raw = path.read_bytes()
    return _decode_json_object_without_duplicates(raw), hashlib.sha256(raw).hexdigest()


def _read_regular_json_snapshot(path: Path) -> Tuple[Dict[str, Any], str]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("migration source is not a regular file")
        chunks = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    payload = _decode_json_object_without_duplicates(raw)
    migrated = dict(payload)
    for key in list(migrated):
        if _name_hash(str(key)) != LEGACY_JSON_KEY_HASH:
            continue
        if CANONICAL_JSON_KEY in migrated and migrated[CANONICAL_JSON_KEY] != migrated[key]:
            raise ValueError("historical JSON key conflicts with canonical key")
        migrated[CANONICAL_JSON_KEY] = migrated.pop(key)
    return migrated, hashlib.sha256(raw).hexdigest()


def _bool_value(value: Any) -> Tuple[Optional[int], bool]:
    if isinstance(value, bool):
        return (1 if value else 0), True
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value), True
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "ein", "ja"}:
        return 1, True
    if text in {"0", "false", "no", "off", "aus", "nein", ""}:
        return 0, True
    return None, False


def _override_value(value: Any) -> Tuple[Optional[str], bool]:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "ein", "central", "zentral"}:
        return "central", True
    if text in {"0", "false", "no", "off", "aus", "local", "lokal", ""}:
        return "local", True
    return None, False


def _ip_value(value: Any) -> Tuple[str, bool]:
    text = str(value or "").strip()
    if not text:
        return "", True
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return "", False
    return text, parsed.version == 4


def _threshold_value(value: Any) -> Tuple[Optional[int], bool]:
    try:
        parsed = int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None, False
    return parsed, 100 <= parsed <= 100000


def _validated_values(raw: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], list[str]]:
    values: Dict[str, Any] = {}
    errors = []
    values["override"], ok = _override_value(raw.get("override"))
    if not ok:
        errors.append("override_invalid")
    values["ip"], ok = _ip_value(raw.get("ip"))
    if not ok:
        errors.append("ip_invalid")
    values["invert"], ok = _bool_value(raw.get("invert"))
    if not ok:
        errors.append("invert_invalid")
    values["dynamic_unblock_enable"], ok = _bool_value(raw.get("dynamic_unblock_enable"))
    if not ok:
        errors.append("dynamic_unblock_invalid")
    values["unblock_threshold_w"], ok = _threshold_value(raw.get("unblock_threshold_w"))
    if not ok:
        errors.append("unblock_threshold_invalid")
    if values.get("override") == "central" and not values.get("ip"):
        errors.append("central_ip_missing")
    return (values if not errors else None), errors


def _canonical_family(config: Mapping[str, Any]) -> Dict[str, Any]:
    present = {suffix: CANONICAL_PREFIX + suffix in config for suffix in CONTRACT_SUFFIXES}
    complete = all(present.values())
    values = None
    errors: list[str] = []
    if complete:
        raw = {suffix: config.get(CANONICAL_PREFIX + suffix) for suffix in CONTRACT_SUFFIXES}
        values, errors = _validated_values(raw)
    return {
        "present_count": sum(1 for value in present.values() if value),
        "complete": complete,
        "absent": not any(present.values()),
        "valid": complete and not errors,
        "values": values,
        "errors": errors,
    }


def _hashed_config_family(config: Mapping[str, Any]) -> Dict[str, Any]:
    matched: Dict[str, tuple[str, Any]] = {}
    duplicate_suffixes = []
    for key, value in config.items():
        suffix = LEGACY_CONFIG_KEY_HASHES.get(_name_hash(str(key)))
        if suffix is None:
            continue
        if suffix in matched:
            duplicate_suffixes.append(suffix)
            continue
        matched[suffix] = (str(key), value)
    raw = {suffix: pair[1] for suffix, pair in matched.items()}
    complete = len(matched) == len(CONTRACT_SUFFIXES) and not duplicate_suffixes
    values = None
    errors: list[str] = []
    if complete:
        values, errors = _validated_values(raw)
    elif matched:
        errors.append("historical_config_partial")
    if duplicate_suffixes:
        errors.append("historical_config_duplicate")
    return {
        "present_count": len(matched),
        "complete": complete,
        "absent": not matched,
        "valid": complete and not errors,
        "values": values,
        "errors": errors,
        "source_keys": [pair[0] for pair in matched.values()],
    }


def resolve_config_contract(
    config: Optional[Dict[str, Any]],
    *,
    clear_persisted_block: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Bewertet den Runtimevertrag und sperrt jeden verbliebenen Alteingang."""
    original = dict(config or {})
    canonical = _canonical_family(original)
    historical = _hashed_config_family(original)
    persisted_block = (
        str(original.get(CONTRACT_STATUS_KEY, "") or "").strip().lower() == "blocked"
        and not clear_persisted_block
    )
    blocked = False
    source = "safe_defaults"
    reason = "canonical_absent"
    values = dict(SAFE_CONTRACT)
    if not historical["absent"]:
        blocked = True
        source = "hash_bound_input"
        if "historical_config_duplicate" in historical["errors"]:
            reason = "historical_config_duplicate"
        elif not historical["complete"]:
            reason = "historical_config_partial"
        elif not historical["valid"]:
            reason = "historical_config_invalid"
        elif canonical["absent"]:
            reason = "migration_required"
        elif not canonical["valid"]:
            reason = "canonical_config_invalid_or_partial"
        elif canonical["values"] != historical["values"]:
            reason = "config_family_conflict"
        else:
            reason = "cleanup_required"
    elif persisted_block:
        blocked = True
        source = "persisted_safe_block"
        reason = str(original.get(CONTRACT_REASON_KEY) or "persisted_conflict")
    elif canonical["valid"]:
        values = dict(canonical["values"])
        source = "canonical"
        reason = "canonical_valid"
    elif not canonical["absent"]:
        blocked = True
        source = "safe_conflict"
        reason = "canonical_invalid" if canonical["errors"] else "canonical_partial"

    resolved = dict(original)
    for suffix, value in values.items():
        resolved[CANONICAL_PREFIX + suffix] = value
    resolved[CONTRACT_STATUS_KEY] = "blocked" if blocked else "ok"
    resolved[CONTRACT_REASON_KEY] = reason
    return resolved, {
        "schema": CONTRACT_SCHEMA,
        "status": "blocked" if blocked else "ok",
        "blocked": blocked,
        "reason": reason,
        "source": source,
        "canonical_present_count": canonical["present_count"],
        "canonical_complete": canonical["complete"],
        "historical_present_count": historical["present_count"],
        "changed": resolved != original,
    }


def effective_contract(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    resolved, status = resolve_config_contract(config)
    values = {suffix: resolved[CANONICAL_PREFIX + suffix] for suffix in CONTRACT_SUFFIXES}
    return {**values, "migration": status, "commands_blocked": bool(status["blocked"])}


def state_migration_required(contract: Mapping[str, Any], *, override_requested: bool) -> bool:
    """Trennt eine konfigurierte Capability vom vollständig unkonfigurierten Fall."""
    migration = contract.get("migration") if isinstance(contract.get("migration"), dict) else {}
    unconfigured = bool(
        migration.get("status") == "ok"
        and migration.get("source") == "safe_defaults"
        and migration.get("reason") == "canonical_absent"
        and migration.get("canonical_present_count") == 0
        and migration.get("historical_present_count") == 0
        and contract.get("override") == "local"
        and not override_requested
    )
    return not unconfigured


def _atomic_write_json(path: Path, payload: Dict[str, Any], *, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp = Path(tmp_name)
        if previous is not None:
            os.chmod(tmp, mode if mode is not None else previous.st_mode & 0o7777)
            try:
                os.chown(tmp, previous.st_uid, previous.st_gid)
            except PermissionError:
                pass
        elif mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        with path.open("r", encoding="utf-8") as handle:
            reloaded = json.load(handle)
        if reloaded != payload:
            raise OSError("atomic JSON verification failed")
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _write_if_changed(path: Path, payload: Dict[str, Any], *, mode: Optional[int] = None) -> bool:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        current = None
    if current == payload:
        if mode is not None and path.exists() and (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        return False
    _atomic_write_json(path, payload, mode=mode)
    return True


def _verified_backup(source: Path, backup_dir: Path, slot: str) -> Path:
    try:
        source_stat = source.lstat()
    except OSError as exc:
        raise OSError("migration backup source metadata unavailable") from exc
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise OSError("migration backup source is not a regular file")
    for directory in (backup_dir.parent.parent, backup_dir.parent, backup_dir):
        if not os.path.lexists(directory):
            continue
        try:
            directory_stat = directory.lstat()
        except OSError as exc:
            raise OSError("migration backup directory metadata unavailable") from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("migration backup directory is unsafe")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for directory in (backup_dir.parent, backup_dir):
        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise OSError("migration backup directory is unsafe")
    os.chmod(backup_dir, 0o700)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, os.O_RDONLY | nofollow)
    try:
        opened_source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(opened_source_stat.st_mode):
            raise OSError("migration backup source is not a regular file")
        source_digest = hashlib.sha256()
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle:
            for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                source_digest.update(block)
            source_handle.seek(0)
            source_hash = source_digest.hexdigest()
            target = backup_dir / f"{slot}-{source_hash[:16]}.bak"
            if os.path.lexists(target):
                target_stat = target.lstat()
                if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                    raise OSError("migration backup target is unsafe")
            else:
                target_fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                )
                with os.fdopen(target_fd, "wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
                    target_handle.flush()
                    os.fchmod(target_handle.fileno(), 0o600)
                    os.fsync(target_handle.fileno())
    finally:
        os.close(source_fd)
    target_stat = target.lstat()
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
        raise OSError("migration backup target is unsafe")
    target_fd = os.open(target, os.O_RDONLY | nofollow)
    try:
        opened_target_stat = os.fstat(target_fd)
        if not stat.S_ISREG(opened_target_stat.st_mode):
            raise OSError("migration backup target is unsafe")
        target_digest = hashlib.sha256()
        with os.fdopen(target_fd, "rb", closefd=False) as target_handle:
            for block in iter(lambda: target_handle.read(1024 * 1024), b""):
                target_digest.update(block)
            os.fsync(target_handle.fileno())
    finally:
        os.close(target_fd)
    if target_digest.hexdigest() != source_hash or stat.S_IMODE(target_stat.st_mode) != 0o600:
        raise OSError("verified migration backup failed")
    try:
        dir_fd = os.open(backup_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return target


def migrate_config_file(
    path: str,
    *,
    backup_dir: Optional[str] = None,
    dry_run: bool = False,
    clear_persisted_block: bool = False,
) -> Dict[str, Any]:
    """Migriert exakt eine vollständig erkannte historische Configfamilie."""
    config_path = Path(path)
    original, original_source_hash = _load_json_object_without_duplicates(config_path)
    canonical = _canonical_family(original)
    historical = _hashed_config_family(original)
    status: Dict[str, Any] = {
        "schema": CONTRACT_SCHEMA,
        "status": "ok",
        "blocked": False,
        "reason": "no_historical_input",
        "source": "canonical" if canonical["valid"] else "safe_defaults",
        "historical_present_count": historical["present_count"],
        "backup_created": False,
        "backup_path": None,
        "changed": False,
    }
    if historical["absent"]:
        _, runtime_status = resolve_config_contract(original, clear_persisted_block=clear_persisted_block)
        status.update({
            "status": runtime_status["status"],
            "blocked": runtime_status["blocked"],
            "reason": runtime_status["reason"],
            "source": runtime_status["source"],
        })
        return status

    backup_root = Path(backup_dir) if backup_dir else config_path.parent / "config_backups"
    target_backup_dir = backup_root / "aux_inverter_migration"
    if not dry_run:
        if _regular_file_hash(config_path) != original_source_hash:
            raise OSError("source_changed_during_migration")
        backup_path = _verified_backup(config_path, target_backup_dir, "aux-config-source")
        if _regular_file_hash(config_path) != original_source_hash or _regular_file_hash(backup_path) != original_source_hash:
            raise OSError("source_changed_during_migration")
        status["backup_created"] = True
        status["backup_path"] = str(backup_path)
        status["backup_sha256"] = _file_hash(backup_path)

    if not historical["valid"]:
        status.update(status="blocked", blocked=True, reason="historical_config_invalid_or_partial", source="hash_bound_input")
        return status
    if not canonical["absent"] and not canonical["valid"]:
        status.update(status="blocked", blocked=True, reason="canonical_config_invalid_or_partial", source="hash_bound_input")
        return status
    if canonical["valid"] and canonical["values"] != historical["values"]:
        status.update(status="blocked", blocked=True, reason="config_family_conflict", source="hash_bound_input")
        return status

    values = dict(canonical["values"] if canonical["valid"] else historical["values"])
    resolved = dict(original)
    for key in historical["source_keys"]:
        resolved.pop(key, None)
    for suffix, value in values.items():
        resolved[CANONICAL_PREFIX + suffix] = value
    resolved[CONTRACT_STATUS_KEY] = "ok"
    resolved[CONTRACT_REASON_KEY] = "hash_bound_input_migrated"
    status.update(
        reason="hash_bound_input_migrated",
        source="hash_bound_input",
        changed=resolved != original,
        source_sha256=original_source_hash,
    )
    if dry_run or resolved == original:
        return status
    if _regular_file_hash(config_path) != original_source_hash:
        raise OSError("source_changed_during_migration")
    _atomic_write_json(config_path, resolved)
    reloaded = json.loads(config_path.read_text(encoding="utf-8"))
    if reloaded != resolved or any(key in reloaded for key in historical["source_keys"]):
        raise OSError("config migration reload verification failed")
    status["target_sha256"] = _file_hash(config_path)
    return status


def _read_json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    migrated = dict(payload)
    for key in list(migrated):
        if _name_hash(str(key)) != LEGACY_JSON_KEY_HASH:
            continue
        if CANONICAL_JSON_KEY in migrated and migrated[CANONICAL_JSON_KEY] != migrated[key]:
            return None
        migrated[CANONICAL_JSON_KEY] = migrated.pop(key)
    return migrated


def _float_values(documents: Iterable[Dict[str, Any]], keys: Iterable[str]) -> Iterable[float]:
    for document in documents:
        for key in keys:
            try:
                value = float(document.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                yield value


def _bool_values(documents: Iterable[Dict[str, Any]], keys: Iterable[str]) -> set:
    values = set()
    for document in documents:
        for key in keys:
            value = document.get(key)
            if isinstance(value, bool):
                values.add(value)
            elif isinstance(value, (int, float)) and value in (0, 1):
                values.add(bool(value))
            elif isinstance(value, str) and value.strip().lower() in {"0", "1", "false", "true", "off", "on"}:
                values.add(value.strip().lower() in {"1", "true", "on"})
    return values


def _strict_bool_alias(document: Dict[str, Any], keys: Iterable[str], *, required: bool) -> Tuple[Optional[bool], bool]:
    values = []
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if not isinstance(value, bool):
            return None, False
        values.append(value)
    if required and not values:
        return None, False
    if len(set(values)) > 1:
        return None, False
    return (values[0] if values else None), True


def _strict_time_alias(document: Dict[str, Any], keys: Iterable[str], *, required: bool) -> Tuple[Optional[float], bool]:
    values = []
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, False
        parsed = float(value)
        if not math.isfinite(parsed) or parsed < 0:
            return None, False
        values.append(parsed)
    if required and not values:
        return None, False
    return (max(values) if values else None), True


def _runtime_document_validation_errors(role: str, document: Dict[str, Any]) -> list[str]:
    errors = []
    if not document:
        return [role + "_empty"]
    if role.endswith("manual_lock"):
        _locked, valid = _strict_bool_alias(document, ("locked",), required=True)
        if not valid:
            errors.append(role + "_locked_invalid")
        _timestamp, valid = _strict_time_alias(document, ("ts", "updated_at", "created_at"), required=False)
        if not valid:
            errors.append(role + "_time_invalid")
        return errors
    if role.endswith("guard"):
        _desired, valid = _strict_bool_alias(document, ("desired_wr_on", "desired_on", "wr_on"), required=True)
        if not valid:
            errors.append(role + "_desired_state_invalid")
        _relay, valid = _strict_bool_alias(document, ("relay_on",), required=True)
        if not valid:
            errors.append(role + "_relay_state_invalid")
        _changed, valid = _strict_time_alias(document, ("last_state_change_ts", "last_switch_ts"), required=True)
        if not valid:
            errors.append(role + "_change_time_invalid")
        _blocked, valid = _strict_time_alias(document, ("block_until", "next_switch_allowed_ts"), required=True)
        if not valid:
            errors.append(role + "_block_time_invalid")
        _sent, valid = _strict_time_alias(document, ("last_send_ts",), required=False)
        if not valid:
            errors.append(role + "_send_time_invalid")
        return errors
    _desired, valid = _strict_bool_alias(document, ("desired_wr_on", "desired_on", "wr_on"), required=True)
    if not valid:
        errors.append(role + "_desired_state_invalid")
    _relay, valid = _strict_bool_alias(document, ("relay_on",), required=True)
    if not valid:
        errors.append(role + "_relay_state_invalid")
    for keys, label in (
        (("ts", "updated_at"), "state_time"),
        (("last_state_change_ts",), "change_time"),
        (("last_send_ts", "last_attempt_ts"), "send_time"),
    ):
        _timestamp, valid = _strict_time_alias(document, keys, required=False)
        if not valid:
            errors.append(role + "_" + label + "_invalid")
    return errors


def _non_executable_placeholder_timestamp(
    document: Dict[str, Any],
) -> Optional[float]:
    """Erkennt ausschließlich einen nachweislich nicht ausführbaren Laufzeitplatzhalter."""
    if document.get("schema") != "direct_marketing_aux_inverter_shelly_v2":
        return None
    allowed_statuses = {"local_fallback", "direct_marketing_disabled", "migration_blocked"}
    if document.get("status") not in allowed_statuses or document.get("command_sent") is not False:
        return None
    required_null = ("desired_wr_on", "desired_on", "relay_on")
    if any(key not in document or document.get(key) is not None for key in required_null):
        return None
    optional_null = ("wr_on", "requested_wr_on", "requested_relay_on")
    if any(key in document and document.get(key) is not None for key in optional_null):
        return None
    if document.get("status") == "migration_blocked":
        if document.get("override_requested") is not False:
            return None
        for key in ("enabled", "control_available", "lock_available", "manual_locked"):
            if key in document and document.get(key) is not False:
                return None
        if document.get("command_status") not in (None, "blocked"):
            return None
        if document.get("error") not in (None, "", "state_contract_blocked"):
            return None
        zero_or_null_history = (
            "last_send_ts",
            "last_attempt_ts",
            "last_state_change_ts",
            "manual_lock_ts",
            "switch_lock_remaining_s",
            "block_until",
            "next_switch_allowed_ts",
        )
        if any(document.get(key) not in (None, 0, 0.0) for key in zero_or_null_history):
            return None
        if any(
            key.startswith("previous_") and value is not None
            for key, value in document.items()
        ):
            return None
        forbidden_history_tokens = ("pending", "retry", "backup")
        if any(
            any(token in key.casefold() for token in forbidden_history_tokens)
            for key in document
        ):
            return None
    timestamp, valid = _strict_time_alias(document, ("ts", "updated_at"), required=True)
    if not valid:
        return None
    return timestamp


def _terminal_marker_placeholder_signature(document: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
    """Bindet nur die sicherheitsrelevante Semantik eines inaktiven Nullplatzhalters."""
    if (
        document.get("status") != "migration_blocked"
        or _non_executable_placeholder_timestamp(document) is None
    ):
        return None
    actuator_keys = (
        "desired_wr_on",
        "desired_on",
        "wr_on",
        "requested_wr_on",
        "requested_relay_on",
        "relay_on",
    )
    return (
        document.get("schema"),
        document.get("status"),
        document.get("command_sent"),
        document.get("override_requested"),
        tuple((key, document.get(key)) for key in actuator_keys),
    )


def _terminal_marker_absorption_readonly(
    *,
    marker_path: Path,
    state_path: Path,
    lock_path: Path,
    guard_path: Path,
    saved_marker: Dict[str, Any],
    historical_paths: Mapping[str, Optional[Path]],
    discovery_reasons: Iterable[str],
    capability_active: bool,
    override_requested: bool,
) -> Optional[Dict[str, Any]]:
    """Überstimmt einen engen inaktiven Altmarker ausschließlich read-only pro Aufruf."""
    if capability_active or override_requested or tuple(discovery_reasons):
        return None
    if any(path is not None for path in historical_paths.values()):
        return None
    if os.path.lexists(lock_path) or os.path.lexists(guard_path):
        return None

    allowed_marker_keys = {
        "schema",
        "status",
        "terminal",
        "blocked",
        "reasons",
        "manual_locked",
        "canonical_placeholder_ignored",
        "historical_guard_block_time_derived",
        "guard_block_until",
        "guard_remaining_s",
        "historical_inputs_found",
        "backups",
        "cleanup_complete",
        "command_sent",
    }
    if set(saved_marker) != allowed_marker_keys:
        return None
    expected_reasons = {
        "canonical_state_desired_state_invalid",
        "canonical_state_relay_state_invalid",
    }
    marker_reasons = saved_marker.get("reasons")
    if (
        saved_marker.get("schema") != STATE_MIGRATION_SCHEMA
        or saved_marker.get("status") != "blocked"
        or saved_marker.get("terminal") is not True
        or saved_marker.get("blocked") is not True
        or saved_marker.get("command_sent") is not False
        or not isinstance(marker_reasons, list)
        or len(marker_reasons) != 2
        or set(marker_reasons) != expected_reasons
        or saved_marker.get("manual_locked") is not False
        or saved_marker.get("canonical_placeholder_ignored") is not False
        or saved_marker.get("historical_guard_block_time_derived") is not False
        or saved_marker.get("guard_block_until") is not None
        or saved_marker.get("guard_remaining_s") != 0
        or saved_marker.get("historical_inputs_found") != []
        or saved_marker.get("cleanup_complete") is not False
    ):
        return None

    try:
        secure_marker, marker_sha256 = _read_regular_json_snapshot(marker_path)
    except (OSError, ValueError):
        return None
    if secure_marker != saved_marker:
        return None

    backups = saved_marker.get("backups")
    if not isinstance(backups, dict) or set(backups) != {"canonical_state"}:
        return None
    reference = backups.get("canonical_state")
    if not isinstance(reference, dict) or set(reference) != {"file", "sha256"}:
        return None
    filename = reference.get("file")
    backup_sha256 = reference.get("sha256")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or not isinstance(backup_sha256, str)
        or len(backup_sha256) != 64
        or any(character not in "0123456789abcdef" for character in backup_sha256)
        or filename != f"slot-canonical_state-{backup_sha256[:16]}.bak"
    ):
        return None

    backup_root = marker_path.parent / "config_backups" / "aux_inverter_migration"
    for directory in (marker_path.parent, backup_root.parent, backup_root):
        try:
            metadata = directory.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return None
    backup_path = backup_root / filename
    try:
        backup_metadata = backup_path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(backup_metadata.st_mode)
        or not stat.S_ISREG(backup_metadata.st_mode)
        or stat.S_IMODE(backup_metadata.st_mode) != 0o600
    ):
        return None

    try:
        backup_document, actual_backup_sha256 = _read_regular_json_snapshot(backup_path)
        state_document, state_sha256 = _read_regular_json_snapshot(state_path)
    except (OSError, ValueError):
        return None
    if actual_backup_sha256 != backup_sha256:
        return None
    state_signature = _terminal_marker_placeholder_signature(state_document)
    backup_signature = _terminal_marker_placeholder_signature(backup_document)
    if state_signature is None or backup_signature is None or state_signature != backup_signature:
        return None

    return {
        "schema": STATE_MIGRATION_SCHEMA,
        "status": "non_applicable_placeholder_history",
        "terminal": False,
        "blocked": False,
        "migration_required": False,
        "reasons": [],
        "cleanup_complete": False,
        "command_sent": False,
        "terminal_marker_absorbed_readonly": True,
        "terminal_marker_sha256": marker_sha256,
        "canonical_state_sha256": state_sha256,
        "canonical_state_backup_sha256": backup_sha256,
        "non_executable_placeholder_roles": ["canonical_state", "canonical_state_backup"],
    }


def _historical_guard_without_block_time_is_derivable(document: Dict[str, Any]) -> bool:
    """Erlaubt nur das belegte Altformat; explizit ungültige Sperrwerte bleiben Fehler."""
    block_keys = ("block_until", "next_switch_allowed_ts")
    if any(key in document for key in block_keys):
        return False
    _changed, valid = _strict_time_alias(
        document,
        ("last_state_change_ts", "last_switch_ts"),
        required=True,
    )
    return valid


def _discover_hashed_path(directory: Path, role: str) -> Tuple[Optional[Path], Optional[str]]:
    expected = [digest for digest, mapped_role in LEGACY_RUNTIME_BASENAME_HASHES.items() if mapped_role == role]
    if len(expected) != 1:
        return None, "historical_hash_contract_invalid"
    try:
        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            return None, f"historical_{role}_directory_unsafe"
        entries = list(directory.iterdir())
    except OSError:
        return None, f"historical_{role}_directory_unreadable"
    matches = []
    for entry in entries:
        if _name_hash(entry.name) != expected[0]:
            continue
        try:
            entry_stat = entry.lstat()
        except OSError:
            return None, f"historical_{role}_entry_unreadable"
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISREG(entry_stat.st_mode):
            return None, f"historical_{role}_entry_unsafe"
        matches.append(entry)
    if len(matches) > 1:
        return None, f"historical_{role}_duplicate"
    return (matches[0] if matches else None), None


def write_canonical_state(path: str, payload: Dict[str, Any], *, mode: int = 0o660) -> None:
    """Schreibt ausschließlich den neutralen kanonischen Zustand."""
    _write_if_changed(Path(path), payload, mode=mode)


def migrate_state_files(
    *,
    canonical_state_path: str,
    canonical_lock_path: str,
    canonical_guard_path: str,
    status_path: str,
    now_s: float,
    config_blocked: bool = False,
    capability_active: bool = True,
    override_requested: bool = False,
    min_switch_interval_s: float = MIN_SWITCH_INTERVAL_S,
) -> Dict[str, Any]:
    """Migriert alte State-/Lock-/Guard-Dateien genau einmal und terminal."""
    state_path = Path(canonical_state_path)
    lock_path = Path(canonical_lock_path)
    guard_path = Path(canonical_guard_path)
    marker_path = Path(status_path)
    if config_blocked:
        return {
            "schema": STATE_MIGRATION_SCHEMA,
            "status": "blocked",
            "terminal": False,
            "blocked": True,
            "reasons": ["config_contract_blocked"],
            "cleanup_complete": False,
            "command_sent": False,
        }
    historical_paths: Dict[str, Optional[Path]] = {}
    reasons = []
    for role, directory in (
        ("state", state_path.parent),
        ("manual_lock", lock_path.parent),
        ("guard", guard_path.parent),
    ):
        found, error = _discover_hashed_path(directory, role)
        historical_paths[role] = found
        if error:
            reasons.append(error)
    saved_marker = _read_json(marker_path)
    if saved_marker and saved_marker.get("schema") == STATE_MIGRATION_SCHEMA and saved_marker.get("terminal") is True:
        absorbed = _terminal_marker_absorption_readonly(
            marker_path=marker_path,
            state_path=state_path,
            lock_path=lock_path,
            guard_path=guard_path,
            saved_marker=saved_marker,
            historical_paths=historical_paths,
            discovery_reasons=reasons,
            capability_active=capability_active,
            override_requested=override_requested,
        )
        if absorbed is not None:
            return absorbed
        drift_reasons = [
            f"post_terminal_historical_{role}_input_drift"
            for role, path in historical_paths.items()
            if path is not None
        ]
        drift_reasons.extend(f"post_terminal_{reason}" for reason in reasons)
        if drift_reasons:
            drift_marker = dict(saved_marker)
            drift_marker.update({
                "status": "blocked",
                "blocked": True,
                "terminal": True,
                "reasons": sorted(set(drift_reasons)),
                "cleanup_complete": False,
                "command_sent": False,
            })
            _write_if_changed(marker_path, drift_marker, mode=0o600)
            return drift_marker
        _write_if_changed(marker_path, saved_marker, mode=0o600)
        return saved_marker

    source_paths = {
        "canonical_state": state_path,
        "canonical_manual_lock": lock_path,
        "canonical_guard": guard_path,
        "historical_state": historical_paths["state"],
        "historical_manual_lock": historical_paths["manual_lock"],
        "historical_guard": historical_paths["guard"],
    }
    source_documents: Dict[str, Dict[str, Any]] = {}
    source_hashes: Dict[str, str] = {}
    source_validation_errors: Dict[str, list[str]] = {}
    source_absent = set()
    for role, path in source_paths.items():
        if path is None or not os.path.lexists(path):
            source_absent.add(role)
            continue
        try:
            document, snapshot_hash = _read_regular_json_snapshot(path)
            source_documents[role] = document
            source_hashes[role] = snapshot_hash
            source_validation_errors[role] = _runtime_document_validation_errors(role, document)
        except ValueError:
            reasons.append(role + "_invalid_or_duplicate_json")
        except OSError:
            reasons.append(role + "_source_unsafe")

    canonical_state = source_documents.get("canonical_state")
    canonical_lock = source_documents.get("canonical_manual_lock")
    canonical_guard = source_documents.get("canonical_guard")
    historical_state = source_documents.get("historical_state")
    historical_lock = source_documents.get("historical_manual_lock")
    historical_guard = source_documents.get("historical_guard")

    placeholder_roles = {
        role
        for role, document in (
            ("canonical_state", canonical_state),
            ("historical_state", historical_state),
        )
        if document is not None
        and _non_executable_placeholder_timestamp(document) is not None
    }
    placeholder_error_sets = {
        "canonical_state": {
            "canonical_state_desired_state_invalid",
            "canonical_state_relay_state_invalid",
        },
        "historical_state": {
            "historical_state_desired_state_invalid",
            "historical_state_relay_state_invalid",
        },
    }
    placeholders_are_exact = bool(placeholder_roles) and all(
        set(source_validation_errors.get(role) or ()) == placeholder_error_sets[role]
        for role in placeholder_roles
    )
    historical_state_is_only_placeholder = bool(
        historical_state is None or "historical_state" in placeholder_roles
    )
    historical_auxiliary_state_absent = historical_lock is None and historical_guard is None
    all_auxiliary_state_absent = bool(
        canonical_lock is None
        and canonical_guard is None
        and historical_auxiliary_state_absent
    )
    migration_blocked_placeholder_present = any(
        document is not None and document.get("status") == "migration_blocked"
        for document in (canonical_state, historical_state)
    )
    canonical_runtime_consistent = True
    if canonical_state is not None and "canonical_state" not in placeholder_roles and canonical_guard is not None:
        canonical_desired, canonical_desired_valid = _strict_bool_alias(
            canonical_state,
            ("desired_wr_on", "desired_on", "wr_on"),
            required=True,
        )
        canonical_relay, canonical_relay_valid = _strict_bool_alias(
            canonical_state,
            ("relay_on",),
            required=True,
        )
        guard_desired, guard_desired_valid = _strict_bool_alias(
            canonical_guard,
            ("desired_wr_on", "desired_on", "wr_on"),
            required=True,
        )
        guard_relay, guard_relay_valid = _strict_bool_alias(
            canonical_guard,
            ("relay_on",),
            required=True,
        )
        canonical_runtime_consistent = bool(
            canonical_desired_valid
            and canonical_relay_valid
            and guard_desired_valid
            and guard_relay_valid
            and canonical_desired == guard_desired
            and canonical_relay == guard_relay
        )
    other_validation_errors = any(
        errors
        for role, errors in source_validation_errors.items()
        if role not in placeholder_roles
    )
    inactive_capability_placeholder = bool(
        not capability_active
        and placeholders_are_exact
        and historical_state_is_only_placeholder
        and historical_auxiliary_state_absent
        and (
            not migration_blocked_placeholder_present
            or not override_requested
        )
        and (
            not migration_blocked_placeholder_present
            or all_auxiliary_state_absent
        )
        and canonical_runtime_consistent
        and not other_validation_errors
        and not reasons
    )
    if inactive_capability_placeholder:
        return {
            "schema": STATE_MIGRATION_SCHEMA,
            "status": "non_applicable_placeholder_history",
            "terminal": False,
            "blocked": False,
            "migration_required": False,
            "reasons": [],
            "non_executable_placeholder_roles": sorted(placeholder_roles),
            "cleanup_complete": False,
            "command_sent": False,
        }

    active_capability_placeholder = bool(
        capability_active
        and placeholders_are_exact
        and historical_state_is_only_placeholder
        and historical_auxiliary_state_absent
        and canonical_runtime_consistent
        and not other_validation_errors
        and not reasons
    )
    if active_capability_placeholder:
        return {
            "schema": STATE_MIGRATION_SCHEMA,
            "status": "blocked",
            "terminal": False,
            "blocked": True,
            "migration_required": True,
            "reasons": ["active_capability_runtime_not_initialized"],
            "cleanup_complete": False,
            "command_sent": False,
        }

    if not capability_active:
        return {
            "schema": STATE_MIGRATION_SCHEMA,
            "status": "blocked",
            "terminal": False,
            "blocked": True,
            "migration_required": True,
            "reasons": ["inactive_capability_runtime_not_non_executable_placeholder"],
            "cleanup_complete": False,
            "command_sent": False,
        }

    canonical_placeholder_ignored = False
    canonical_placeholder_ts = (
        _non_executable_placeholder_timestamp(canonical_state)
        if canonical_state is not None
        else None
    )
    historical_state_ts, historical_state_time_valid = (
        _strict_time_alias(historical_state, ("ts", "updated_at"), required=True)
        if historical_state is not None
        else (None, False)
    )
    canonical_placeholder_errors = {
        "canonical_state_desired_state_invalid",
        "canonical_state_relay_state_invalid",
    }
    if (
        canonical_placeholder_ts is not None
        and historical_state is not None
        and historical_state_time_valid
        and historical_state_ts is not None
        and historical_state_ts > canonical_placeholder_ts
        and not source_validation_errors.get("historical_state")
        and set(source_validation_errors.get("canonical_state") or ()) == canonical_placeholder_errors
    ):
        source_validation_errors["canonical_state"] = []
        canonical_state = None
        canonical_placeholder_ignored = True

    historical_guard_block_time_derived = False
    historical_guard_errors = source_validation_errors.get("historical_guard") or []
    if (
        historical_guard is not None
        and historical_guard_errors == ["historical_guard_block_time_invalid"]
        and _historical_guard_without_block_time_is_derivable(historical_guard)
    ):
        source_validation_errors["historical_guard"] = []
        historical_guard_block_time_derived = True

    for validation_errors in source_validation_errors.values():
        reasons.extend(validation_errors)

    backup_dir = marker_path.parent / "config_backups" / "aux_inverter_migration"
    backups: Dict[str, Dict[str, str]] = {}
    backup_sources: Dict[str, Tuple[Path, Path]] = {}
    for role, path in source_paths.items():
        if path is None or role not in source_hashes:
            continue
        try:
            backup = _verified_backup(path, backup_dir, "slot-" + role)
            backup_hash = _regular_file_hash(backup)
            if backup_hash != source_hashes[role] or _regular_file_hash(path) != source_hashes[role]:
                reasons.append("source_changed_during_migration")
                continue
            backups[role] = {"file": backup.name, "sha256": backup_hash}
            backup_sources[role] = (path, backup)
        except OSError:
            reasons.append(role + "_backup_failed")

    for role, path in source_paths.items():
        if path is None:
            continue
        try:
            if role in source_absent:
                if os.path.lexists(path):
                    reasons.append("source_changed_during_migration")
            elif role in source_hashes and _regular_file_hash(path) != source_hashes[role]:
                reasons.append("source_changed_during_migration")
        except OSError:
            reasons.append("source_changed_during_migration")

    state_docs = [doc for doc in (canonical_state, historical_state) if doc]
    lock_docs = [doc for doc in (canonical_lock, historical_lock) if doc]
    guard_docs = [doc for doc in (canonical_guard, historical_guard) if doc]
    manual_locked = any(doc.get("locked") is True for doc in lock_docs)
    lock_payload = None
    if lock_docs:
        lock_ts = max(_float_values(lock_docs, ("ts", "updated_at", "created_at")), default=0.0)
        lock_payload = {
            "schema": "direct_marketing_aux_inverter_shelly_manual_lock_v1",
            "locked": bool(manual_locked),
            "ts": int(lock_ts or now_s),
            "source": "migration",
        }

    guard_last_change = max(_float_values(guard_docs, ("last_state_change_ts", "last_switch_ts")), default=0.0)
    guard_block_until = max(_float_values(guard_docs, ("block_until", "next_switch_allowed_ts")), default=0.0)
    if guard_last_change > 0:
        guard_block_until = max(guard_block_until, guard_last_change + float(min_switch_interval_s))
    guard_wr_values = _bool_values(guard_docs, ("desired_wr_on", "desired_on", "wr_on"))
    guard_relay_values = _bool_values(guard_docs, ("relay_on",))
    if len(guard_wr_values) > 1 or len(guard_relay_values) > 1:
        reasons.append("guard_logical_state_conflict")
    guard_payload = None
    if guard_docs:
        guard_payload = {
            "schema": "direct_marketing_aux_inverter_shelly_guard_v1",
            "last_state_change_ts": guard_last_change,
            "last_send_ts": max(_float_values(guard_docs, ("last_send_ts",)), default=0.0),
            "block_until": guard_block_until,
            "next_switch_allowed_ts": guard_block_until,
            "desired_wr_on": next(iter(guard_wr_values)) if len(guard_wr_values) == 1 else None,
            "relay_on": next(iter(guard_relay_values)) if len(guard_relay_values) == 1 else None,
            "migration_conflict": bool(reasons),
            "source": "migration",
        }

    state_wr_values = _bool_values(state_docs, ("desired_wr_on", "desired_on", "wr_on"))
    state_relay_values = _bool_values(state_docs, ("relay_on",))
    if len(state_wr_values) > 1 or len(state_relay_values) > 1:
        reasons.append("runtime_logical_state_conflict")
    state_payload = None
    if state_docs:
        latest_state = max(state_docs, key=lambda item: max(_float_values((item,), ("ts", "updated_at")), default=0.0))
        desired_state, _valid = _strict_bool_alias(latest_state, ("desired_wr_on", "desired_on", "wr_on"), required=True)
        relay_state, _valid = _strict_bool_alias(latest_state, ("relay_on",), required=True)
        state_ts, _valid = _strict_time_alias(latest_state, ("ts", "updated_at"), required=False)
        change_ts, _valid = _strict_time_alias(latest_state, ("last_state_change_ts",), required=False)
        send_ts, _valid = _strict_time_alias(latest_state, ("last_send_ts",), required=False)
        attempt_ts, _valid = _strict_time_alias(latest_state, ("last_attempt_ts",), required=False)
        state_payload = {
            "schema": "direct_marketing_aux_inverter_shelly_v2",
            "ts": int(state_ts or now_s),
            "desired_wr_on": desired_state,
            "desired_on": desired_state,
            "relay_on": relay_state,
            "last_state_change_ts": change_ts or 0.0,
            "last_send_ts": send_ts or 0.0,
            "last_attempt_ts": attempt_ts or send_ts or 0.0,
            "migration_conflict": bool(reasons),
            "source": "migration",
        }

    for role, path in source_paths.items():
        if path is None:
            continue
        try:
            if role in source_absent:
                if os.path.lexists(path):
                    reasons.append("source_changed_during_migration")
            elif role in source_hashes and _regular_file_hash(path) != source_hashes[role]:
                reasons.append("source_changed_during_migration")
        except OSError:
            reasons.append("source_changed_during_migration")

    blocked = bool(reasons)
    status: Dict[str, Any] = {
        "schema": STATE_MIGRATION_SCHEMA,
        "status": "blocked" if blocked else "complete",
        "terminal": True,
        "blocked": blocked,
        "reasons": sorted(set(reasons)),
        "manual_locked": bool(manual_locked),
        "canonical_placeholder_ignored": canonical_placeholder_ignored,
        "historical_guard_block_time_derived": historical_guard_block_time_derived,
        "guard_block_until": guard_block_until or None,
        "guard_remaining_s": max(0, int(round(guard_block_until - now_s))) if guard_block_until else 0,
        "historical_inputs_found": sorted(role for role, path in historical_paths.items() if path is not None),
        "backups": backups,
        "cleanup_complete": False,
        "command_sent": False,
    }
    if blocked:
        _atomic_write_json(marker_path, status, mode=0o600)
        return status

    if lock_payload is not None:
        write_canonical_state(str(lock_path), lock_payload, mode=0o660)
    if guard_payload is not None:
        write_canonical_state(str(guard_path), guard_payload, mode=0o660)
    if state_payload is not None:
        write_canonical_state(str(state_path), state_payload, mode=0o664)
    for path, expected in (
        (lock_path, lock_payload),
        (guard_path, guard_payload),
        (state_path, state_payload),
    ):
        if expected is not None and _read_json(path) != expected:
            raise OSError("canonical migration reload verification failed")

    removed: list[Tuple[str, Path]] = []
    try:
        for role, path in historical_paths.items():
            if path is not None and path.exists():
                source_role = "historical_" + role
                if source_role not in source_hashes or _regular_file_hash(path) != source_hashes[source_role]:
                    raise OSError("source_changed_during_migration")
                path.unlink()
                removed.append((role, path))
                try:
                    dir_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
        status["cleanup_complete"] = True
        status["canonical_state_present"] = state_path.exists()
        status["canonical_lock_present"] = lock_path.exists()
        status["canonical_guard_present"] = guard_path.exists()
        _atomic_write_json(marker_path, status, mode=0o600)
    except Exception:
        for role, path in removed:
            backup_entry = backup_sources.get("historical_" + role)
            if backup_entry is None:
                continue
            _source, backup = backup_entry
            shutil.copyfile(backup, path)
            os.chmod(path, 0o600)
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        raise
    return status
