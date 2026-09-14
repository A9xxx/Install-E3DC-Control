"""Privates, dauerhaftes WP-Energiekonto ohne Hardware- oder Managerimporte."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import pwd
import stat
import time
import uuid

try:
    from .heatpump_pv_contract import validate_heatpump_pv_state
except ImportError:
    from heatpump_pv_contract import validate_heatpump_pv_state


CHECKPOINT_BASENAME = "heatpump_pv_energy_state.json"
CHECKPOINT_SCHEMA = "heatpump_pv_checkpoint_v1"
COMMAND_BASENAME = "heatpump_pv_command_state.json"
COMMAND_CHECKPOINT_SCHEMA = "heatpump_pv_command_checkpoint_v1"
COMMAND_STATE_SCHEMA = "heatpump_pv_command_state_v1"
CHECKPOINT_HEARTBEAT_S = 60.0
MAX_CHECKPOINT_BYTES = 1024 * 1024
_LAST_DURABLE = {}


class HeatpumpCheckpointError(RuntimeError):
    pass


def _now(value):
    current = time.time() if value is None else float(value)
    if not math.isfinite(current) or current <= 0:
        raise HeatpumpCheckpointError("clock_invalid")
    return current


def _quarantine(reason, now_s):
    # Ein nichtleerer ungültiger Kontostand aktiviert im reinen Vertragsmodul
    # die Rekonsiliation. Auch eine gelöschte Datei darf keinen neuen Topf öffnen.
    return {"schema": "heatpump_pv_checkpoint_quarantine_v1",
            "quarantined": True, "checkpoint_reason": reason,
            "checkpoint_observed_ts": now_s}


def _default_directory():
    try:
        from . import docker_runtime_identity
    except ImportError:
        import docker_runtime_identity
    if docker_runtime_identity.is_docker_runtime_process():
        return docker_runtime_identity.runtime_control_state_directory()
    if os.path.exists("/.dockerenv"):
        raise HeatpumpCheckpointError("docker_runtime_binding_missing")
    return os.path.join(pwd.getpwuid(os.geteuid()).pw_dir, ".e3dc-control-state")


def _open_private_directory(directory=None):
    uid = os.geteuid()
    try:
        web_uid = pwd.getpwnam("www-data").pw_uid
    except KeyError:
        web_uid = 33
    if uid == web_uid:
        raise HeatpumpCheckpointError("web_user_forbidden")
    path = os.path.abspath(os.fspath(directory) if directory is not None else _default_directory())
    components = path.split(os.sep)
    if len(components) < 2 or path == os.sep:
        raise HeatpumpCheckpointError("directory_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(os.sep, flags)
    try:
        parts = [part for part in components if part]
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            if leaf:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                mode = stat.S_IMODE(info.st_mode)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
                    raise HeatpumpCheckpointError("directory_owner_invalid")
                if leaf:
                    if info.st_uid != uid or mode != 0o700:
                        raise HeatpumpCheckpointError("private_directory_mode_invalid")
                elif mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX):
                    raise HeatpumpCheckpointError("directory_parent_writable")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return path, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HeatpumpCheckpointError("checkpoint_duplicate_key")
        result[key] = value
    return result


def _read_checkpoint(directory_fd, *, basename=CHECKPOINT_BASENAME,
                     schema=CHECKPOINT_SCHEMA, validator=validate_heatpump_pv_state):
    descriptor = os.open(basename, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                         dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_CHECKPOINT_BYTES):
            raise HeatpumpCheckpointError("checkpoint_file_unsafe")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_CHECKPOINT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_CHECKPOINT_BYTES:
                raise HeatpumpCheckpointError("checkpoint_too_large")
        envelope = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
        state = validator(envelope.get("state")) if isinstance(envelope, dict) else {}
        if (not state or envelope.get("schema") != schema
                or envelope.get("sha256") != hashlib.sha256(_canonical(envelope.get("state"))).hexdigest()):
            raise HeatpumpCheckpointError("checkpoint_invalid")
        if envelope.get("saved_ts") is None:
            raise HeatpumpCheckpointError("checkpoint_timestamp_missing")
        _now(envelope.get("saved_ts"))
        return state, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    finally:
        os.close(descriptor)


def load_heatpump_pv_checkpoint(*, directory=None, now_s=None):
    """Fehlend/korrupt/unsicher ergibt Quarantäne, niemals einen leeren Starttopf."""
    current = _now(now_s)
    descriptor = None
    try:
        _, descriptor = _open_private_directory(directory)
        state, _ = _read_checkpoint(descriptor)
        return copy.deepcopy(state)
    except FileNotFoundError:
        return _quarantine("checkpoint_missing", current)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        return _quarantine("checkpoint_untrusted", current)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _transition_signature(state):
    # Zähler und Uhrproben gehören in den Heartbeat. Neue Zusagen und Zustands-
    # kanten werden vor ihrer Veröffentlichung dauerhaft geschrieben.
    keys = ("request_id", "revision", "active_command", "cycle_owned", "signal_withdrawn",
            "withdrawal_pending", "running_since_s", "last_stop_s", "issued_ts", "offered_ts",
            "max_power_w", "uncertain_state",
            "energy_guard_pending", "energy_guard_sources", "withdrawal_reason",
            "quarantine_reason", "compressor_running")
    value = {key: state.get(key) for key in keys}
    value["clock_boot_id"] = (state.get("clock") or {}).get("boot_id")
    return hashlib.sha256(_canonical(value)).hexdigest()


def persist_heatpump_pv_checkpoint(state, *, directory=None, now_s=None, force=False):
    """Schreibt atomar mit fsync; True bindet auch eine unveränderte haltbare Zusage.

    ``force`` bezeichnet die Grenze vor Veröffentlichung einer Zusage. Eine
    identische bereits haltbar bestätigte Zusage muss dort nicht erneut auf
    Flash geschrieben werden. Jede Erhöhung ihrer Reservierung schreibt sofort.
    """
    return _persist_checkpoint(state, directory=directory, now_s=now_s, force=force)


def _persist_checkpoint(state, *, directory=None, now_s=None, force=False,
                        basename=CHECKPOINT_BASENAME, schema=CHECKPOINT_SCHEMA,
                        validator=validate_heatpump_pv_state, signature_fn=_transition_signature,
                        quarantine_required=True, heartbeat_s=CHECKPOINT_HEARTBEAT_S):
    descriptor = None
    temporary = None
    try:
        current = _now(now_s)
        validated = validator(state)
        if not validated:
            return False
        path, descriptor = _open_private_directory(directory)
        existing_token = None
        try:
            existing, existing_token = _read_checkpoint(descriptor, basename=basename, schema=schema, validator=validator)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
            # Der erste haltbare Zustand nach Datenverlust muss ausdrücklich
            # dessen Rekonsiliation bewahren; ein normaler neuer Topf ist gesperrt.
            if quarantine_required and validated.get("uncertain_state") is not True:
                return False
            try:
                info = os.stat(basename, dir_fd=descriptor, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    return False
                if not quarantine_required:
                    # Einen beschädigten Kanalbeleg nicht durch eine neue
                    # Startabsicht ersetzen: seine alte Wirkung ist unbekannt.
                    return False
            except FileNotFoundError:
                pass
        signature = signature_fn(validated)
        cache_key = (path, basename)
        previous = _LAST_DURABLE.get(cache_key)
        if (previous and existing_token == previous["token"]
                and previous["signature"] == signature
                and validated.get("battery_reserved_wh", 0) <= previous["battery_reserved_wh"]
                and validated.get("grid_reserved_wh", 0) <= previous["grid_reserved_wh"]
                and 0 <= current - previous["saved_ts"] < heartbeat_s):
            return True
        envelope = {"schema": schema, "saved_ts": current,
                    "sha256": hashlib.sha256(_canonical(validated)).hexdigest(), "state": validated}
        payload = _canonical(envelope)
        if len(payload) > MAX_CHECKPOINT_BYTES:
            return False
        temporary = ".heatpump-pv-" + uuid.uuid4().hex + ".tmp"
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
        with os.fdopen(output, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, basename, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        temporary = None
        os.fsync(descriptor)
        persisted, token = _read_checkpoint(descriptor, basename=basename, schema=schema, validator=validator)
        if _canonical(persisted) != _canonical(validated):
            return False
        _LAST_DURABLE[cache_key] = {"signature": signature, "token": token, "saved_ts": current,
                                   "battery_reserved_wh": validated.get("battery_reserved_wh", 0),
                                   "grid_reserved_wh": validated.get("grid_reserved_wh", 0)}
        return True
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        return False
    finally:
        if descriptor is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except OSError:
                    pass
            os.close(descriptor)


def validate_heatpump_pv_command(state):
    if not isinstance(state, dict) or state.get("schema") != COMMAND_STATE_SCHEMA:
        return {}
    value = copy.deepcopy(state)
    if (not isinstance(value.get("request_id"), str) or not value["request_id"]
            or len(value["request_id"]) > 160 or type(value.get("revision")) is not int
            or value["revision"] < 0):
        return {}
    for key in ("prepared_ts", "issued_ts", "withdrawal_prepared_ts"):
        number = value.get(key)
        if key == "prepared_ts" and number is None:
            return {}
        if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                                   or not math.isfinite(number) or number <= 0):
            return {}
    channels = value.get("channels")
    if not isinstance(channels, dict) or not channels or not set(channels).issubset({"hz", "ww"}):
        return {}
    for channel in channels.values():
        if not isinstance(channel, dict) or channel.get("active") is not True:
            return {}
        target = channel.get("target_c")
        if (isinstance(target, bool) or not isinstance(target, (int, float))
                or not math.isfinite(target) or not 0 <= target <= 100):
            return {}
    for key in ("confirmed", "withdrawal_confirmed", "withdrawal_requested"):
        if key in value and type(value[key]) is not bool:
            return {}
    return value


def load_heatpump_pv_command_checkpoint(*, directory=None, now_s=None):
    """Lädt ausschließlich die Kanal-Intentdatei, niemals das Energiekonto."""
    current = _now(now_s)
    descriptor = None
    try:
        _, descriptor = _open_private_directory(directory)
        command, _ = _read_checkpoint(descriptor, basename=COMMAND_BASENAME,
                                     schema=COMMAND_CHECKPOINT_SCHEMA, validator=validate_heatpump_pv_command)
        return command
    except FileNotFoundError:
        return {}
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        return _quarantine("command_checkpoint_untrusted", current)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def persist_heatpump_pv_command_checkpoint(command, *, directory=None, now_s=None, force=False):
    """Intent vor dem Ausgang fsyncen; stabile Kanalbelege schreiben nicht zyklisch."""
    return _persist_checkpoint(
        command, directory=directory, now_s=now_s, force=force,
        basename=COMMAND_BASENAME, schema=COMMAND_CHECKPOINT_SCHEMA,
        validator=validate_heatpump_pv_command,
        signature_fn=lambda value: hashlib.sha256(_canonical(value)).hexdigest(),
        quarantine_required=False, heartbeat_s=math.inf,
    )
