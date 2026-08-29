#!/bin/bash
# E3DC-Control V4 Service Launcher

set -euo pipefail
umask 022

readonly ENV="/usr/bin/env" SYSTEMCTL="/usr/bin/systemctl"
readonly PYTHON3="/usr/bin/python3" FLOCK="/usr/bin/flock" INSTALL="/usr/bin/install"
readonly MKDIR="/usr/bin/mkdir" STAT="/usr/bin/stat" SLEEP="/usr/bin/sleep"
readonly MATTER_SERVICE="e3dc-matter-bridge.service" MATTER_RESET_ACTION="reset-matter-pairing"
readonly MATTER_LOCK_DIR="/run/e3dc-control/locks"
readonly MATTER_LOCK_FILE="${MATTER_LOCK_DIR}/matter-service.lock"
readonly UPDATE_LOCK_ROOT="/run/lock"
readonly UPDATE_LOCK_DIR="${UPDATE_LOCK_ROOT}/e3dc-control"
readonly UPDATE_LOCK_FILE="${UPDATE_LOCK_DIR}/update.lock"
readonly MATTER_CONFIG_FILE="/var/www/html/data/e3dc_v4.json"

if (( EUID != 0 )); then
    printf 'Der Service-Launcher darf nur mit Root-Rechten ausgeführt werden.\n' >&2
    exit 77
fi

if (( $# != 2 )); then
    printf 'Aufruf: %s <start|stop|restart|status|enable|disable|reset-matter-pairing> <service>\n' "$0" >&2
    exit 64
fi

readonly ACTION="$1"
REQUESTED_SERVICE="$2"

case "$ACTION" in
    start|stop|restart|status|enable|disable)
        ;;
    reset-matter-pairing)
        if [[ "$REQUESTED_SERVICE" != "$MATTER_SERVICE" ]]; then
            printf 'Der Matter-Reset akzeptiert ausschließlich den fest gebundenen Matter-Dienst.\n' >&2
            exit 64
        fi
        ;;
    *)
        printf 'Unzulässige Aktion: %s\n' "$ACTION" >&2
        exit 64
        ;;
esac

if [[ ! "$REQUESTED_SERVICE" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
    printf 'Unzulässiger Dienst: %s\n' "$REQUESTED_SERVICE" >&2
    exit 64
fi

if [[ "$ACTION" != "$MATTER_RESET_ACTION" && "$REQUESTED_SERVICE" != *.service ]]; then
    REQUESTED_SERVICE="${REQUESTED_SERVICE}.service"
fi
readonly SERVICE="$REQUESTED_SERVICE"

readonly -a ALLOWED_SERVICES=(
    "e3dc-live.service"
    "energy_manager.service"
    "e3dc-wallbox-manager.service"
    "e3dc-epex-manager.service"
    "e3dc-weather-manager.service"
    "e3dc-storage-simulator.service"
    "e3dc-storage-manager.service"
    "e3dc-ha.service"
    "e3dc-matter-bridge.service"
    "e3dc-bluelink.service"
    "e3dc-lux-live.service"
    "e3dc-idm-live.service"
    "e3dc-stiebel-live.service"
    "e3dc-dimplex-live.service"
    "e3dc-heizstab.service"
    "e3dc-climate-live.service"
    "e3dc-climate-control.service"
    "e3dc-forecast-evidence.service"
    "e3dc-notifier.service"
    "e3dc-mqtt-hub.service"
    "e3dc-websocket.service"
    "e3dc-shadow-sync.service"
)

SERVICE_OK=0
for S in "${ALLOWED_SERVICES[@]}"; do
    if [ "$SERVICE" == "$S" ]; then
        SERVICE_OK=1
        break
    fi
done

if [ $SERVICE_OK -eq 0 ]; then
    printf "Dienst '%s' ist nicht für die Ausführung über den Web-Launcher freigegeben.\n" "$SERVICE" >&2
    exit 64
fi

if [[ ! -x "$ENV" || ! -x "$SYSTEMCTL" ]]; then
    printf 'Fest gebundene Systemprogramme sind nicht ausführbar.\n' >&2
    exit 126
fi

acquire_matter_lock() {
    local directory=""
    local named_before=""
    local opened=""
    local named_after=""
    for directory in "/run" "/run/e3dc-control" "$MATTER_LOCK_DIR"; do
        if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
            return 1
        fi
        if [[ "$directory" != "/run" ]]; then
            "$INSTALL" -d -o root -g root -m 0755 -- "$directory"
        fi
        if [[ ! -d "$directory" || -L "$directory" ]] \
            || [[ "$("$STAT" -c '%u:%g:%a' -- "$directory")" != "0:0:755" ]]; then
            return 1
        fi
    done
    if [[ -L "$MATTER_LOCK_FILE" || ( -e "$MATTER_LOCK_FILE" && ! -f "$MATTER_LOCK_FILE" ) ]]; then
        return 1
    fi
    if [[ ! -e "$MATTER_LOCK_FILE" ]]; then
        (umask 077; set -o noclobber; : > "$MATTER_LOCK_FILE") 2>/dev/null || true
    fi
    if [[ ! -f "$MATTER_LOCK_FILE" || -L "$MATTER_LOCK_FILE" ]]; then
        return 1
    fi
    named_before="$("$STAT" -c '%d:%i:%u:%g:%a:%h' -- "$MATTER_LOCK_FILE")"
    if [[ "$named_before" != *":0:0:600:1" ]]; then
        return 1
    fi
    exec 9<> "$MATTER_LOCK_FILE"
    opened="$("$STAT" -Lc '%d:%i:%u:%g:%a:%h' -- /proc/self/fd/9)"
    named_after="$("$STAT" -c '%d:%i:%u:%g:%a:%h' -- "$MATTER_LOCK_FILE")"
    if [[ "$opened" != "$named_before" || "$named_after" != "$named_before" ]]; then
        return 1
    fi
    if ! "$FLOCK" -w 60 -x 9; then
        return 1
    fi
}

MATTER_UPDATE_LOCK_ERROR="UPDATE_LOCK"
acquire_matter_update_lock() {
    local directory=""
    local metadata=""
    local mode=""
    local owner=""
    local group=""
    local named_before=""
    local opened=""
    local named_after=""
    MATTER_UPDATE_LOCK_ERROR="UPDATE_LOCK"
    for directory in "/run" "$UPDATE_LOCK_ROOT"; do
        if [[ -L "$directory" || ! -d "$directory" ]]; then
            return 1
        fi
        metadata="$("$STAT" -c '%u:%g:%a' -- "$directory")"
        IFS=: read -r owner group mode <<< "$metadata"
        if [[ "$owner:$group" != "0:0" || ! "$mode" =~ ^[0-7]+$ ]]; then
            return 1
        fi
        mode=$((8#$mode))
        if [[ "$directory" == "$UPDATE_LOCK_ROOT" ]]; then
            if (( (mode & 0002) != 0 && (mode & 01000) == 0 )); then
                return 1
            fi
            if (( (mode & 01000) == 0 && (mode & 0022) != 0 )); then
                return 1
            fi
        elif (( (mode & 0022) != 0 )); then
            return 1
        fi
    done
    if [[ -L "$UPDATE_LOCK_DIR" || ( -e "$UPDATE_LOCK_DIR" && ! -d "$UPDATE_LOCK_DIR" ) ]]; then
        return 1
    fi
    if [[ ! -e "$UPDATE_LOCK_DIR" ]]; then
        "$MKDIR" -m 0700 -- "$UPDATE_LOCK_DIR" 2>/dev/null || true
    fi
    if [[ ! -d "$UPDATE_LOCK_DIR" || -L "$UPDATE_LOCK_DIR" ]] \
        || [[ "$("$STAT" -c '%u:%g:%a' -- "$UPDATE_LOCK_DIR")" != "0:0:700" ]]; then
        return 1
    fi
    if [[ -L "$UPDATE_LOCK_FILE" || ( -e "$UPDATE_LOCK_FILE" && ! -f "$UPDATE_LOCK_FILE" ) ]]; then
        return 1
    fi
    if [[ ! -e "$UPDATE_LOCK_FILE" ]]; then
        (umask 077; set -o noclobber; : > "$UPDATE_LOCK_FILE") 2>/dev/null || true
    fi
    if [[ ! -f "$UPDATE_LOCK_FILE" || -L "$UPDATE_LOCK_FILE" ]]; then
        return 1
    fi
    named_before="$("$STAT" -c '%d:%i:%u:%g:%a:%h' -- "$UPDATE_LOCK_FILE")"
    if [[ "$named_before" != *":0:0:600:1" ]]; then
        return 1
    fi
    exec 8<> "$UPDATE_LOCK_FILE"
    opened="$("$STAT" -Lc '%d:%i:%u:%g:%a:%h' -- /proc/self/fd/8)"
    named_after="$("$STAT" -c '%d:%i:%u:%g:%a:%h' -- "$UPDATE_LOCK_FILE")"
    if [[ "$opened" != "$named_before" || "$named_after" != "$named_before" ]]; then
        return 1
    fi
    if ! "$FLOCK" -n -x 8; then
        MATTER_UPDATE_LOCK_ERROR="UPDATE_BUSY"
        return 1
    fi
}

systemctl_value() {
    "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$SYSTEMCTL" show --no-pager --property="$1" --value -- "$SERVICE"
}

matter_config_state() {
    "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$PYTHON3" -I -B - "$MATTER_CONFIG_FILE" <<'PY'
import json
import os
import stat
import sys

MAX_CONFIG_BYTES = 1024 * 1024

def fail():
    raise SystemExit(1)

def open_parent(path):
    normalized = os.path.normpath(str(path))
    if not normalized.startswith("/") or normalized != str(path) or normalized == "/":
        fail()
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        fail()
    flags = os.O_RDONLY | nofollow | directory | cloexec
    descriptor = os.open("/", flags)
    try:
        for component in normalized.split("/")[1:]:
            if not component or component in {".", ".."}:
                fail()
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                fail()
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                    or not stat.S_ISDIR(opened.st_mode)):
                os.close(child)
                fail()
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

def identity(meta):
    return (
        int(meta.st_dev), int(meta.st_ino), int(meta.st_mode), int(meta.st_nlink),
        int(meta.st_uid), int(meta.st_gid), int(meta.st_size),
        int(meta.st_mtime_ns), int(meta.st_ctime_ns),
    )

try:
    config_path = str(sys.argv[1])
    parent_path, name = os.path.split(config_path)
    if not name or name in {".", ".."}:
        fail()
    parent = open_parent(parent_path)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size < 2 or before.st_size > MAX_CONFIG_BYTES):
            fail()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if identity(opened) != identity(before):
                fail()
            payload = bytearray()
            while len(payload) < opened.st_size:
                chunk = os.read(descriptor, min(65536, opened.st_size - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (len(payload) != before.st_size
                or identity(after) != identity(before)
                or identity(rebound) != identity(before)):
            fail()
    finally:
        os.close(parent)
    document = json.loads(bytes(payload).decode("utf-8"))
    if not isinstance(document, dict):
        fail()
    value = document.get("matter_bridge")
    enabled = str(value).strip().lower() in {"1", "true", "yes", "on"}
    print("enabled" if enabled else "disabled")
except (KeyError, OSError, TypeError, UnicodeError, ValueError):
    fail()
PY
}

matter_reset_error() {
    case "$1" in
        LOCK|UPDATE_*|RUNTIME|CONFIG_BINDING|UNIT_*|STOP_FAILED|INACTIVE_UNCONFIRMED|QUARANTINE_COLLISION|STORAGE_*|PAIRING_*|START_*|INTERNAL)
            printf 'MATTER_RESET_ERROR_%s\n' "$1" >&2 ;;
        *) printf 'MATTER_RESET_ERROR_INTERNAL\n' >&2 ;;
    esac
}

matter_start_error() {
    local code="$1" active_state="" sub_state=""
    if ! "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$SYSTEMCTL" stop -- "$SERVICE" 2>/dev/null \
        || ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
        || ! sub_state="$(systemctl_value SubState 2>/dev/null)" \
        || [[ "$active_state:$sub_state" != "inactive:dead" ]]; then
        code="START_STOP_FAILED"
    fi
    matter_reset_error "$code"
}

restart_matter_service() {
    local active_state=""
    local sub_state=""
    local stable_round=""
    "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$SYSTEMCTL" reset-failed -- "$SERVICE" 2>/dev/null || true
    if ! "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$SYSTEMCTL" start -- "$SERVICE"; then
        matter_start_error START_FAILED
        return 1
    fi
    for stable_round in 1 2 3; do
        "$SLEEP" 1
        if ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
            || ! sub_state="$(systemctl_value SubState 2>/dev/null)" \
            || [[ "$active_state" != "active" || "$sub_state" != "running" ]]; then
            matter_start_error START_UNSTABLE
            return 1
        fi
    done
}

reset_matter_pairing() {
    local load_state=""
    local active_state=""
    local sub_state=""
    local unit_file_state=""
    local unit_user="-"
    local unit_group="www-data"
    local current_unit_user=""
    local current_unit_group=""
    local config_state=""
    local was_active=0
    local unit_restart_capable=0
    local restart_allowed=0
    if [[ ! -x "$PYTHON3" || ! -x "$SLEEP" ]]; then
        matter_reset_error RUNTIME
        return 126
    fi
    if ! load_state="$(systemctl_value LoadState 2>/dev/null)"; then
        matter_reset_error UNIT_QUERY
        return 1
    fi
    case "$load_state" in loaded|not-found) ;; *)
        matter_reset_error UNIT_LOAD_STATE
        return 1 ;;
    esac
    if [[ "$load_state" == "loaded" ]]; then
        if ! unit_file_state="$(systemctl_value UnitFileState 2>/dev/null)" \
            || ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
            || ! sub_state="$(systemctl_value SubState 2>/dev/null)" \
            || ! unit_user="$(systemctl_value User 2>/dev/null)" \
            || ! unit_group="$(systemctl_value Group 2>/dev/null)"; then
            matter_reset_error UNIT_CONTRACT
            return 1
        fi
        case "$unit_file_state" in masked|masked-runtime)
            matter_reset_error UNIT_MASKED
            return 1 ;;
        esac
        case "$unit_file_state" in
            enabled|enabled-runtime) unit_restart_capable=1 ;;
            disabled|static|indirect|generated|transient|linked|linked-runtime|alias)
                unit_restart_capable=0 ;;
            *)
                matter_reset_error UNIT_STATE
                return 1 ;;
        esac
        case "$active_state:$sub_state" in
            active:running) was_active=1 ;;
            inactive:dead) was_active=0 ;;
            *)
            matter_reset_error UNIT_STATE
            return 1 ;;
        esac
        if [[ -z "$unit_user" || -z "$unit_group" ]]; then
            matter_reset_error UNIT_OWNER
            return 1
        fi
    else
        if ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
            || [[ "$active_state" != "inactive" ]]; then
            matter_reset_error UNIT_STATE
            return 1
        fi
    fi

    if ! config_state="$(matter_config_state 2>/dev/null)"; then
        matter_reset_error CONFIG_BINDING
        return 1
    fi
    case "$config_state" in
        enabled|disabled) ;;
        *)
            matter_reset_error CONFIG_BINDING
            return 1 ;;
    esac
    if (( was_active == 1 && unit_restart_capable == 1 )) \
        && [[ "$config_state" == "enabled" ]]; then
        restart_allowed=1
    fi

    if (( was_active == 1 )); then
        if ! "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
            "$SYSTEMCTL" stop -- "$SERVICE"; then
            matter_reset_error STOP_FAILED
            return 1
        fi
        if ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
            || ! sub_state="$(systemctl_value SubState 2>/dev/null)" \
            || [[ "$active_state" != "inactive" || "$sub_state" != "dead" ]]; then
            matter_reset_error INACTIVE_UNCONFIRMED
            return 1
        fi
    fi

    if ! "$ENV" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
        "$PYTHON3" -I -B - "$unit_user" "$unit_group" "$load_state" <<'PY'
# E3DC_MATTER_RESET_PYTHON_BEGIN
import ctypes
import errno
import grp
import json
import os
import pwd
import stat
import sys
import time

STORAGE_PATH = "/var/www/html/data/matter-storage"
PAIRING_PATH = "/var/www/html/ramdisk/matter_pairing.json"
QUARANTINE_NAME = ".matter-storage-reset-quarantine"
QUARANTINE_PREPARE_NAME = ".matter-storage-reset-quarantine.prepare"
TRANSACTION_STAGE_PREFIX = ".matter-storage-reset-stage-"
MARKER_NAME = ".e3dc-matter-reset-transaction.json"
TRANSACTION_SCHEMA = "e3dc_matter_reset_transaction_v1"
TRANSACTION_OWNER_UID = 0
TRANSACTION_OWNER_GID = 0
TRANSACTION_MARKER_MAX_BYTES = 1024
RESET_TRANSACTION_SECONDS = 75.0

class ResetError(RuntimeError):
    pass

def mount_id(fd):
    try:
        with open(f"/proc/self/fdinfo/{int(fd)}", "r", encoding="ascii") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator and key.strip() == "mnt_id":
                    parsed = int(value.strip(), 10)
                    if parsed > 0:
                        return parsed
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResetError("mount") from exc
    raise ResetError("mount")

def flags():
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise ResetError("flags")
    return nofollow, directory, cloexec

def path_flags():
    nofollow, _, cloexec = flags()
    path_only = getattr(os, "O_PATH", 0)
    if not path_only:
        raise ResetError("flags")
    return path_only | nofollow | cloexec

def open_dir(path):
    normalized = os.path.normpath(str(path))
    if not normalized.startswith("/") or normalized != str(path) or normalized == "/":
        raise ResetError("path")
    nofollow, directory, cloexec = flags()
    open_flags = os.O_RDONLY | nofollow | directory | cloexec
    fd = os.open("/", open_flags)
    try:
        for component in normalized.split("/")[1:]:
            if not component or component in {".", ".."}:
                raise ResetError("path")
            named = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise ResetError("parent")
            child = os.open(component, open_flags, dir_fd=fd)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                os.close(child)
                raise ResetError("parent")
            mount_id(child)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise

def resolve_uid(token):
    if token == "-":
        return None
    try:
        entry = pwd.getpwuid(int(token, 10)) if token.isdecimal() else pwd.getpwnam(token)
    except (KeyError, ValueError) as exc:
        raise ResetError("user") from exc
    if entry.pw_uid <= 0:
        raise ResetError("user")
    return int(entry.pw_uid)

def resolve_gid(token):
    try:
        entry = grp.getgrgid(int(token, 10)) if token.isdecimal() else grp.getgrnam(token)
        web = grp.getgrnam("www-data")
    except (KeyError, ValueError) as exc:
        raise ResetError("group") from exc
    if entry.gr_gid <= 0 or entry.gr_gid != web.gr_gid:
        raise ResetError("group")
    return int(entry.gr_gid)

def named(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None

def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ResetError("deadline")

def private_binding(meta, entry_mount):
    return {"dev": int(meta.st_dev), "ino": int(meta.st_ino),
            "type": stat.S_IFMT(meta.st_mode), "mount": int(entry_mount),
            "names": (), "children": {}, "name": ""}

def verify_private_identity(meta, binding, entry_mount):
    if stat.S_ISREG(meta.st_mode) and meta.st_nlink != 1:
        raise ResetError("hardlink")
    if ((meta.st_dev, meta.st_ino) != (binding["dev"], binding["ino"])
            or stat.S_IFMT(meta.st_mode) != binding["type"]
            or entry_mount != binding["mount"]):
        raise ResetError("tree")

def scan_mount_tree(root_fd, deadline):
    check_deadline(deadline)
    nofollow, directory, cloexec = flags()
    directory_flags = os.O_RDONLY | nofollow | directory | cloexec
    root_meta = os.fstat(root_fd)
    root_mount = mount_id(root_fd)
    root_binding = private_binding(root_meta, root_mount)
    records = [root_binding]
    seen_directories = {(int(root_meta.st_dev), int(root_meta.st_ino))}
    current = os.dup(root_fd)
    frames = []
    try:
        root_binding["names"] = tuple(sorted(os.listdir(current)))
        frames.append({"record": 0, "offset": 0})
        while frames:
            check_deadline(deadline)
            frame = frames[-1]
            record = records[frame["record"]]
            names = record["names"]
            if frame["offset"] >= len(names):
                if tuple(sorted(os.listdir(current))) != names:
                    raise ResetError("tree")
                if len(frames) == 1:
                    break
                child_record = record
                parent_record = records[frames[-2]["record"]]
                parent_fd = os.open("..", directory_flags, dir_fd=current)
                try:
                    verify_private_identity(
                        os.fstat(parent_fd), parent_record, mount_id(parent_fd)
                    )
                    rebound = os.stat(
                        child_record["name"],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    verify_private_identity(rebound, child_record, root_mount)
                except Exception:
                    os.close(parent_fd)
                    raise
                os.close(current)
                current = parent_fd
                frames.pop()
                continue
            name = names[frame["offset"]]
            frame["offset"] += 1
            meta = os.stat(name, dir_fd=current, follow_symlinks=False)
            path_fd = os.open(name, path_flags(), dir_fd=current)
            try:
                opened = os.fstat(path_fd)
                entry_mount = mount_id(path_fd)
                if ((opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
                        or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(meta.st_mode)
                        or entry_mount != root_mount):
                    raise ResetError(
                        "foreign-mount" if entry_mount != root_mount else "tree"
                    )
                is_directory = stat.S_ISDIR(opened.st_mode)
                is_regular = stat.S_ISREG(opened.st_mode)
                if not (is_directory or is_regular):
                    raise ResetError("tree")
                if is_regular and opened.st_nlink != 1:
                    raise ResetError("hardlink")
                child = private_binding(opened, entry_mount)
                child["name"] = name
                child_index = len(records)
                records.append(child)
                record["children"][name] = child_index
                if not is_directory:
                    continue
                identity = (int(opened.st_dev), int(opened.st_ino))
                if identity in seen_directories:
                    raise ResetError("tree")
                seen_directories.add(identity)
                child_fd = os.open(name, directory_flags, dir_fd=current)
                try:
                    verify_private_identity(os.fstat(child_fd), child, mount_id(child_fd))
                    child["names"] = tuple(sorted(os.listdir(child_fd)))
                except Exception:
                    os.close(child_fd)
                    raise
            finally:
                os.close(path_fd)
            os.close(current)
            current = child_fd
            frames.append({"record": child_index, "offset": 0})
        return records
    finally:
        os.close(current)

def clear_private_tree(root_fd, root_mount, deadline):
    records = scan_mount_tree(root_fd, deadline)
    if records[0]["mount"] != root_mount:
        raise ResetError("foreign-mount")
    nofollow, directory, cloexec = flags()
    directory_flags = os.O_RDONLY | nofollow | directory | cloexec
    current = os.dup(root_fd)
    frames = [{"record": 0, "offset": 0}]
    try:
        while frames:
            check_deadline(deadline)
            frame = frames[-1]
            record = records[frame["record"]]
            names = record["names"]
            remaining = names[frame["offset"]:]
            if tuple(sorted(os.listdir(current))) != remaining:
                raise ResetError("tree")
            if not remaining:
                if len(frames) == 1:
                    break
                child_record = record
                parent_record = records[frames[-2]["record"]]
                parent_fd = os.open("..", directory_flags, dir_fd=current)
                try:
                    verify_private_identity(
                        os.fstat(parent_fd), parent_record, mount_id(parent_fd)
                    )
                    child_name = child_record["name"]
                    rebound = os.stat(
                        child_name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    verify_private_identity(rebound, child_record, root_mount)
                    check_deadline(deadline)
                    os.rmdir(child_name, dir_fd=parent_fd)
                    check_deadline(deadline)
                    if named(parent_fd, child_name) is not None:
                        raise ResetError("clear")
                    os.fsync(parent_fd)
                    check_deadline(deadline)
                except Exception:
                    os.close(parent_fd)
                    raise
                os.close(current)
                current = parent_fd
                frames.pop()
                continue
            name = remaining[0]
            child_index = record["children"].get(name)
            if not isinstance(child_index, int):
                raise ResetError("tree")
            child = records[child_index]
            frame["offset"] += 1
            meta = os.stat(name, dir_fd=current, follow_symlinks=False)
            path_fd = os.open(name, path_flags(), dir_fd=current)
            try:
                opened = os.fstat(path_fd)
                verify_private_identity(opened, child, mount_id(path_fd))
                verify_private_identity(meta, child, root_mount)
                is_directory = stat.S_ISDIR(meta.st_mode) and not stat.S_ISLNK(meta.st_mode)
                if is_directory:
                    child_fd = os.open(name, directory_flags, dir_fd=current)
                    try:
                        verify_private_identity(
                            os.fstat(child_fd), child, mount_id(child_fd)
                        )
                    except Exception:
                        os.close(child_fd)
                        raise
                else:
                    rebound = os.stat(name, dir_fd=current, follow_symlinks=False)
                    verify_private_identity(rebound, child, root_mount)
                    check_deadline(deadline)
                    os.unlink(name, dir_fd=current)
                    check_deadline(deadline)
                    if named(current, name) is not None:
                        raise ResetError("clear")
                    os.fsync(current)
                    check_deadline(deadline)
            finally:
                os.close(path_fd)
            if is_directory:
                os.close(current)
                current = child_fd
                frames.append({"record": child_index, "offset": 0})
    finally:
        os.close(current)

def rename_noreplace(source_parent, source_name, target_parent, target_name):
    if (not source_name or not target_name
            or "/" in source_name or "/" in target_name
            or source_name in {".", ".."} or target_name in {".", ".."}):
        raise ResetError("flags")
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise ResetError("flags")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        int(source_parent),
        os.fsencode(source_name),
        int(target_parent),
        os.fsencode(target_name),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ResetError("collision")
    raise OSError(error, os.strerror(error))

def identity_record(meta, entry_mount):
    return {
        "dev": int(meta.st_dev),
        "ino": int(meta.st_ino),
        "mount_id": int(entry_mount),
    }

def strict_identity_record(value):
    if not isinstance(value, dict) or set(value) != {"dev", "ino", "mount_id"}:
        raise ResetError("collision")
    if any(type(value[key]) is not int or value[key] <= 0 for key in value):
        raise ResetError("collision")
    return value

def same_identity(meta, entry_mount, expected):
    return identity_record(meta, entry_mount) == strict_identity_record(expected)

def open_private_transaction_directory(parent, name, harden=False):
    meta = named(parent, name)
    if meta is None:
        return None
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
        raise ResetError("collision")
    nofollow, directory, cloexec = flags()
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        parent_meta = os.fstat(parent)
        if harden:
            os.fchown(descriptor, TRANSACTION_OWNER_UID, TRANSACTION_OWNER_GID)
            os.fchmod(descriptor, 0o700)
            opened = os.fstat(descriptor)
        rebound = named(parent, name)
        if (rebound is None
                or (opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
                or marker_meta(opened) != marker_meta(rebound)
                or opened.st_dev != parent_meta.st_dev
                or mount_id(descriptor) != mount_id(parent)
                or (opened.st_uid, opened.st_gid, stat.S_IMODE(opened.st_mode)) != (
                    TRANSACTION_OWNER_UID, TRANSACTION_OWNER_GID, 0o700)):
            raise ResetError("collision")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def marker_meta(meta):
    return (
        int(meta.st_dev), int(meta.st_ino), stat.S_IFMT(meta.st_mode),
        int(meta.st_nlink), int(meta.st_uid), int(meta.st_gid),
        stat.S_IMODE(meta.st_mode), int(meta.st_size),
    )

def bind_transaction_marker(container, link_count=1):
    meta = named(container, MARKER_NAME)
    if meta is None:
        return None
    if (stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode)
            or meta.st_nlink != link_count
            or meta.st_uid != TRANSACTION_OWNER_UID
            or meta.st_gid != TRANSACTION_OWNER_GID
            or stat.S_IMODE(meta.st_mode) != 0o600
            or meta.st_size < 2 or meta.st_size > TRANSACTION_MARKER_MAX_BYTES):
        raise ResetError("collision")
    descriptor = os.open(
        MARKER_NAME,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=container,
    )
    try:
        opened = os.fstat(descriptor)
        if marker_meta(opened) != marker_meta(meta):
            raise ResetError("collision")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(65536, opened.st_size - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(
            MARKER_NAME,
            dir_fd=container,
            follow_symlinks=False,
        )
        if (len(payload) != opened.st_size
                or marker_meta(after) != marker_meta(opened)
                or marker_meta(rebound) != marker_meta(opened)):
            raise ResetError("collision")
        marker_payload = bytes(payload)
        try:
            document = json.loads(marker_payload.decode("ascii"))
            canonical = (
                json.dumps(document, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":")) + "\n"
            ).encode("ascii")
        except (TypeError, UnicodeError, ValueError) as exc:
            raise ResetError("collision") from exc
        if marker_payload != canonical:
            raise ResetError("collision")
    except Exception:
        os.close(descriptor)
        raise
    return {
        "fd": descriptor,
        "container": container,
        "dev": int(opened.st_dev),
        "ino": int(opened.st_ino),
        "size": int(opened.st_size),
        "document": document,
    }

def verify_marker(marker):
    opened = os.fstat(marker["fd"])
    rebound = os.stat(
        MARKER_NAME,
        dir_fd=marker["container"],
        follow_symlinks=False,
    )
    if ((opened.st_dev, opened.st_ino) != (marker["dev"], marker["ino"])
            or (rebound.st_dev, rebound.st_ino) != (marker["dev"], marker["ino"])
            or marker_meta(opened) != marker_meta(rebound)
            or opened.st_size != marker["size"]):
        raise ResetError("collision")

def transaction_document(parent, qfd, source_fd, source_name):
    return {
        "schema": TRANSACTION_SCHEMA,
        "parent": identity_record(os.fstat(parent), mount_id(parent)),
        "quarantine": {
            "name": QUARANTINE_NAME,
            **identity_record(os.fstat(qfd), mount_id(qfd)),
        },
        "source": {
            "name": source_name,
            "quarantine_name": "storage",
            **identity_record(os.fstat(source_fd), mount_id(source_fd)),
        },
    }

def validate_transaction_document(document, parent, qfd=None):
    if (not isinstance(document, dict)
            or set(document) != {"schema", "parent", "quarantine", "source"}
            or document.get("schema") != TRANSACTION_SCHEMA):
        raise ResetError("collision")
    if not same_identity(os.fstat(parent), mount_id(parent), document["parent"]):
        raise ResetError("collision")
    quarantine = document["quarantine"]
    if (not isinstance(quarantine, dict)
            or set(quarantine) != {"name", "dev", "ino", "mount_id"}
            or quarantine.get("name") != QUARANTINE_NAME):
        raise ResetError("collision")
    strict_identity_record({key: quarantine[key] for key in ("dev", "ino", "mount_id")})
    source = document["source"]
    if (not isinstance(source, dict)
            or set(source) != {
                "name", "quarantine_name", "dev", "ino", "mount_id"
            }
            or source.get("name") != "matter-storage"
            or source.get("quarantine_name") != "storage"):
        raise ResetError("collision")
    strict_identity_record({key: source[key] for key in ("dev", "ino", "mount_id")})
    if qfd is not None and not same_identity(
        os.fstat(qfd),
        mount_id(qfd),
        {key: quarantine[key] for key in ("dev", "ino", "mount_id")},
    ):
        raise ResetError("collision")
    return document

def write_transaction_marker(container, document):
    payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    if len(payload) > TRANSACTION_MARKER_MAX_BYTES:
        raise ResetError("collision")
    descriptor = os.open(
        MARKER_NAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=container,
    )
    try:
        os.fchown(descriptor, TRANSACTION_OWNER_UID, TRANSACTION_OWNER_GID)
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise ResetError("collision")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(container)
    marker = bind_transaction_marker(container)
    if marker is None or marker["document"] != document:
        raise ResetError("collision")
    return marker

def bind_transaction_open(parent, deadline, owned):
    check_deadline(deadline)
    qfd = open_private_transaction_directory(parent, QUARANTINE_NAME)
    if qfd is not None:
        owned.append(qfd)
    prepare_fd = open_private_transaction_directory(parent, QUARANTINE_PREPARE_NAME)
    if prepare_fd is not None:
        owned.append(prepare_fd)
    quarantine_named = (
        named(qfd, MARKER_NAME)
        if qfd is not None else None
    )
    parent_named = named(parent, MARKER_NAME)
    dual_link = any(
        item is not None and item.st_nlink == 2
        for item in (quarantine_named, parent_named)
    )
    if dual_link:
        if (prepare_fd is not None or qfd is None
                or quarantine_named is None or parent_named is None
                or (quarantine_named.st_dev, quarantine_named.st_ino)
                != (parent_named.st_dev, parent_named.st_ino)
                or quarantine_named.st_nlink != 2 or parent_named.st_nlink != 2
                or set(os.listdir(qfd)) != {MARKER_NAME}):
            raise ResetError("collision")
        internal = bind_transaction_marker(qfd, 2)
        external = None
        try:
            external = bind_transaction_marker(parent, 2)
            if (internal["document"] != external["document"]
                    or (internal["dev"], internal["ino"])
                    != (external["dev"], external["ino"])):
                raise ResetError("collision")
            validate_transaction_document(
                external["document"], parent, qfd
            )
            verify_marker(internal)
            verify_marker(external)
            os.fsync(parent)
            os.unlink(MARKER_NAME, dir_fd=qfd)
            os.fsync(qfd)
        finally:
            os.close(internal["fd"])
            if external is not None:
                os.close(external["fd"])
    parent_marker = bind_transaction_marker(parent)
    if parent_marker is not None:
        owned.append(parent_marker["fd"])
    if qfd is not None and prepare_fd is not None:
        raise ResetError("collision")
    if prepare_fd is not None:
        if qfd is not None or parent_marker is not None:
            raise ResetError("collision")
        marker = bind_transaction_marker(prepare_fd)
        if marker is not None:
            owned.append(marker["fd"])
        if marker is None or set(os.listdir(prepare_fd)) != {MARKER_NAME}:
            raise ResetError("collision")
        document = validate_transaction_document(marker["document"], parent, prepare_fd)
        verify_marker(marker)
        rename_noreplace(parent, QUARANTINE_PREPARE_NAME, parent, QUARANTINE_NAME)
        os.fsync(parent)
        published = named(parent, QUARANTINE_NAME)
        if (published is None or (published.st_dev, published.st_ino) != (
                os.fstat(prepare_fd).st_dev, os.fstat(prepare_fd).st_ino)):
            raise ResetError("collision")
        owned.remove(prepare_fd)
        owned.remove(marker["fd"])
        return {"phase":"active", "qfd":prepare_fd, "marker":marker,
                "document":document}
    if qfd is not None:
        internal_marker = bind_transaction_marker(qfd)
        if internal_marker is not None:
            owned.append(internal_marker["fd"])
        if parent_marker is not None:
            if internal_marker is not None or os.listdir(qfd):
                raise ResetError("collision")
            document = validate_transaction_document(
                parent_marker["document"], parent, qfd
            )
            owned.remove(qfd)
            owned.remove(parent_marker["fd"])
            return {
                "phase": "receipt",
                "qfd": qfd,
                "marker": parent_marker,
                "document": document,
            }
        if internal_marker is None:
            raise ResetError("collision")
        entries = set(os.listdir(qfd))
        if not entries.issubset({MARKER_NAME, "storage"}):
            raise ResetError("collision")
        document = validate_transaction_document(
            internal_marker["document"], parent, qfd
        )
        owned.remove(qfd)
        owned.remove(internal_marker["fd"])
        return {
            "phase": "active",
            "qfd": qfd,
            "marker": internal_marker,
            "document": document,
        }
    if parent_marker is not None:
        document = validate_transaction_document(parent_marker["document"], parent)
        owned.remove(parent_marker["fd"])
        return {
            "phase": "receipt",
            "qfd": None,
            "marker": parent_marker,
            "document": document,
        }
    return None

def bind_transaction(parent, deadline):
    owned = []
    try:
        return bind_transaction_open(parent, deadline, owned)
    finally:
        for descriptor in owned:
            os.close(descriptor)

def create_transaction(parent, source_fd, source_name, deadline):
    check_deadline(deadline)
    if any(named(parent, candidate) is not None for candidate in (
        QUARANTINE_NAME,
        QUARANTINE_PREPARE_NAME,
        MARKER_NAME,
    )):
        raise ResetError("collision")
    stage_name = ""
    for _ in range(32):
        candidate = TRANSACTION_STAGE_PREFIX + os.urandom(16).hex()
        try:
            os.mkdir(candidate, 0o700, dir_fd=parent)
            stage_name = candidate
            break
        except FileExistsError:
            continue
    if not stage_name:
        raise ResetError("collision")
    prepare_fd = open_private_transaction_directory(parent, stage_name, True)
    if prepare_fd is None:
        raise ResetError("collision")
    marker = None
    try:
        os.fsync(prepare_fd)
        os.fsync(parent)
        document = transaction_document(parent, prepare_fd, source_fd, source_name)
        marker = write_transaction_marker(prepare_fd, document)
        verify_marker(marker)
        published = named(parent, stage_name)
        if (published is None or marker_meta(os.fstat(prepare_fd)) != marker_meta(published)
                or set(os.listdir(prepare_fd)) != {MARKER_NAME}):
            raise ResetError("collision")
        rename_noreplace(
            parent,
            stage_name,
            parent,
            QUARANTINE_PREPARE_NAME,
        )
        os.fsync(parent)
        published = os.stat(
            QUARANTINE_PREPARE_NAME, dir_fd=parent, follow_symlinks=False
        )
        if (published.st_dev, published.st_ino) != (
            os.fstat(prepare_fd).st_dev, os.fstat(prepare_fd).st_ino
        ):
            raise ResetError("collision")
        rename_noreplace(
            parent,
            QUARANTINE_PREPARE_NAME,
            parent,
            QUARANTINE_NAME,
        )
        os.fsync(parent)
        published = os.stat(QUARANTINE_NAME, dir_fd=parent, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (
            os.fstat(prepare_fd).st_dev, os.fstat(prepare_fd).st_ino
        ):
            raise ResetError("collision")
        return {
            "phase": "active",
            "qfd": prepare_fd,
            "marker": marker,
            "document": document,
        }
    except Exception:
        if marker is not None:
            os.close(marker["fd"])
        os.close(prepare_fd)
        raise

def open_bound_source(container, name, expected):
    meta = named(container, name)
    if meta is None:
        return None
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
        raise ResetError("collision")
    nofollow, directory, cloexec = flags()
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=container,
    )
    opened = os.fstat(descriptor)
    if ((opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
            or not same_identity(opened, mount_id(descriptor), expected)):
        os.close(descriptor)
        raise ResetError("collision")
    return descriptor

def transaction_source(transaction, parent, root_name):
    if transaction is None or transaction["phase"] != "active":
        return None, "none"
    document = transaction["document"]
    expected = {
        key: document["source"][key] for key in ("dev", "ino", "mount_id")
    }
    qfd = transaction["qfd"]
    moved = open_bound_source(qfd, "storage", expected)
    if moved is not None:
        current = named(parent, root_name)
        if current is not None and same_identity(current, mount_id(parent), expected):
            os.close(moved)
            raise ResetError("collision")
        return moved, "quarantine"
    current_meta = named(parent, root_name)
    if current_meta is not None and (
        int(current_meta.st_dev), int(current_meta.st_ino)
    ) == (int(expected["dev"]), int(expected["ino"])):
        current = open_bound_source(parent, root_name, expected)
        if current is not None:
            return current, "parent"
    return None, "cleared"

def quarantine_storage(parent, root_name, root_fd, qfd, deadline):
    check_deadline(deadline)
    root = os.fstat(root_fd)
    parent_meta = os.fstat(parent)
    root_meta = os.stat(root_name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(root_meta.st_mode)
            or (root_meta.st_dev, root_meta.st_ino) != (root.st_dev, root.st_ino)
            or root.st_dev != parent_meta.st_dev
            or mount_id(root_fd) != mount_id(parent)
            or set(os.listdir(qfd)) != {MARKER_NAME}):
        raise ResetError("root-swap")
    scan_mount_tree(root_fd, deadline)
    rebound = os.stat(root_name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(rebound.st_mode)
            or (rebound.st_dev, rebound.st_ino) != (root.st_dev, root.st_ino)):
        raise ResetError("root-swap")
    try:
        check_deadline(deadline)
        rename_noreplace(parent, root_name, qfd, "storage")
    except OSError as exc:
        raise ResetError("foreign-mount") from exc
    check_deadline(deadline)
    if named(parent, root_name) is not None:
        raise ResetError("root-swap")
    moved = os.stat("storage", dir_fd=qfd, follow_symlinks=False)
    if ((moved.st_dev, moved.st_ino) != (root.st_dev, root.st_ino)
            or not stat.S_ISDIR(moved.st_mode)):
        raise ResetError("root-swap")
    os.fsync(qfd)
    os.fsync(parent)
    check_deadline(deadline)

def move_marker_to_receipt(parent, transaction, deadline):
    qfd = transaction["qfd"]
    marker = transaction["marker"]
    verify_marker(marker)
    if set(os.listdir(qfd)) != {MARKER_NAME}:
        raise ResetError("collision")
    check_deadline(deadline)
    receipt = None
    try:
        try:
            os.link(
                MARKER_NAME, MARKER_NAME,
                src_dir_fd=qfd, dst_dir_fd=parent,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise ResetError("collision") from exc
        receipt = bind_transaction_marker(parent, 2)
        if (receipt is None
                or (receipt["dev"], receipt["ino"]) != (marker["dev"], marker["ino"])
                or receipt["document"] != marker["document"]):
            raise ResetError("collision")
        validate_transaction_document(receipt["document"], parent, qfd)
        verify_marker(marker)
        verify_marker(receipt)
        os.fsync(parent)
        os.unlink(MARKER_NAME, dir_fd=qfd)
        os.fsync(qfd)
        marker_descriptor = marker["fd"]
        marker["fd"] = -1
        os.close(marker_descriptor)
    except Exception:
        if receipt is not None:
            os.close(receipt["fd"])
        raise
    transaction["marker"] = receipt
    transaction["phase"] = "receipt"
    check_deadline(deadline)

def finish_transaction_receipt(parent, transaction, deadline):
    marker = transaction["marker"]
    qfd = transaction["qfd"]
    verify_marker(marker)
    if qfd is not None:
        opened = os.fstat(qfd)
        rebound = os.stat(
            QUARANTINE_NAME,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if (not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (rebound.st_dev, rebound.st_ino)
                or os.listdir(qfd)):
            raise ResetError("collision")
        validate_transaction_document(marker["document"], parent, qfd)
        check_deadline(deadline)
        os.rmdir(QUARANTINE_NAME, dir_fd=parent)
        os.fsync(parent)
        transaction["qfd"] = None
        os.close(qfd)
    verify_marker(marker)
    check_deadline(deadline)
    os.unlink(MARKER_NAME, dir_fd=parent)
    os.fsync(parent)
    os.close(marker["fd"])
    transaction["marker"] = None
    check_deadline(deadline)

def bind_removable(
    parent, name, uid, gid, require_regular_owner, error_token, allow_hardlink=False
):
    meta = named(parent, name)
    if meta is None:
        return None
    parent_meta = os.fstat(parent)
    parent_mount = mount_id(parent)
    is_regular = stat.S_ISREG(meta.st_mode) and not stat.S_ISLNK(meta.st_mode)
    if (stat.S_ISDIR(meta.st_mode) or meta.st_dev != parent_meta.st_dev
            or (is_regular and meta.st_nlink != 1 and not allow_hardlink)
            or (is_regular and require_regular_owner
                and (meta.st_uid != uid or meta.st_gid != gid))):
        if is_regular and meta.st_nlink != 1 and not allow_hardlink:
            raise ResetError(
                "pairing-hardlink" if error_token == "pairing" else "hardlink"
            )
        raise ResetError(error_token)
    fd = os.open(name, path_flags(), dir_fd=parent)
    opened = os.fstat(fd)
    if ((opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
            or stat.S_IFMT(opened.st_mode) != stat.S_IFMT(meta.st_mode)
            or opened.st_nlink != meta.st_nlink or mount_id(fd) != parent_mount):
        os.close(fd)
        raise ResetError(error_token)
    return {"fd": fd, "parent": parent, "name": name, "dev": int(opened.st_dev),
            "ino": int(opened.st_ino), "mount": parent_mount,
            "uid": int(opened.st_uid), "gid": int(opened.st_gid),
            "mode": stat.S_IMODE(opened.st_mode), "type": stat.S_IFMT(opened.st_mode),
            "nlink": int(opened.st_nlink), "size": int(opened.st_size),
            "error": error_token}

def verify_removable(bound):
    opened = os.fstat(bound["fd"])
    meta = os.stat(bound["name"], dir_fd=bound["parent"], follow_symlinks=False)
    if ((opened.st_dev, opened.st_ino) != (bound["dev"], bound["ino"])
            or (meta.st_dev, meta.st_ino) != (bound["dev"], bound["ino"])
            or stat.S_IFMT(opened.st_mode) != bound["type"]
            or stat.S_IFMT(meta.st_mode) != bound["type"]
            or opened.st_nlink != bound["nlink"] or meta.st_nlink != bound["nlink"]
            or mount_id(bound["fd"]) != bound["mount"] or opened.st_uid != bound["uid"]
            or opened.st_gid != bound["gid"] or stat.S_IMODE(opened.st_mode) != bound["mode"]
            or opened.st_size != bound["size"]):
        raise ResetError(bound["error"])

def unlink_removable(bound):
    verify_removable(bound)
    os.unlink(bound["name"], dir_fd=bound["parent"])
    if named(bound["parent"], bound["name"]) is not None:
        raise ResetError("pairing-clear" if bound["error"] == "pairing" else "clear")
    os.fsync(bound["parent"])

def open_storage_root(parent, name):
    meta = named(parent, name)
    if meta is None:
        return None
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISDIR(meta.st_mode):
        return None
    nofollow, directory, cloexec = flags()
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow | directory | cloexec,
        dir_fd=parent,
    )
    opened = os.fstat(descriptor)
    parent_meta = os.fstat(parent)
    if ((opened.st_dev, opened.st_ino) != (meta.st_dev, meta.st_ino)
            or opened.st_dev != parent_meta.st_dev
            or mount_id(descriptor) != mount_id(parent)):
        os.close(descriptor)
        raise ResetError("foreign-mount")
    return descriptor

def verify_storage_owner(descriptor, uid, gid):
    opened = os.fstat(descriptor)
    if (opened.st_uid != uid or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) != 0o700):
        raise ResetError("storage")

def ensure_replacement(parent, name, uid, gid, allow_root_recovery, deadline):
    check_deadline(deadline)
    meta = named(parent, name)
    created = None
    if meta is None:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = named(parent, name)
    descriptor = open_storage_root(parent, name)
    if descriptor is None:
        raise ResetError("storage")
    try:
        opened = os.fstat(descriptor)
        if created is not None:
            if (created.st_dev, created.st_ino) != (opened.st_dev, opened.st_ino):
                raise ResetError("storage")
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o700)
            opened = os.fstat(descriptor)
        if os.listdir(descriptor):
            raise ResetError("storage")
        identity = (opened.st_uid, opened.st_gid, stat.S_IMODE(opened.st_mode))
        parent_meta = os.fstat(parent)
        exact = identity == (uid, gid, 0o700)
        recoverable = allow_root_recovery and (
            identity == (TRANSACTION_OWNER_UID, TRANSACTION_OWNER_GID, 0o700)
            or (parent_meta.st_uid == uid
                and stat.S_IMODE(parent_meta.st_mode) in {0o2770, 0o2775}
                and identity == (TRANSACTION_OWNER_UID, parent_meta.st_gid, 0o2700))
        )
        if not exact and not recoverable:
            raise ResetError("storage")
        if not exact:
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        os.fsync(parent)
        verify_storage_owner(descriptor, uid, gid)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        final = os.fstat(descriptor)
        if ((rebound.st_dev, rebound.st_ino) != (final.st_dev, final.st_ino)
                or os.listdir(descriptor)):
            raise ResetError("storage")
        check_deadline(deadline)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def reset():
    deadline = time.monotonic() + RESET_TRANSACTION_SECONDS
    requested_uid = resolve_uid(sys.argv[1])
    gid = resolve_gid(sys.argv[2])
    load_state = sys.argv[3]
    if load_state not in {"loaded", "not-found"}:
        raise ResetError("state")
    storage_parent_path, storage_name = os.path.split(STORAGE_PATH)
    pairing_parent_path, pairing_name = os.path.split(PAIRING_PATH)
    storage_parent = open_dir(storage_parent_path)
    pairing_parent = open_dir(pairing_parent_path)
    replacement_root_fd = -1
    source_fd = -1
    transaction = None
    pairing = None
    quarantined = False
    try:
        transaction = bind_transaction(storage_parent, deadline)
        source_location = "none"
        if transaction is not None:
            source_fd, source_location = transaction_source(
                transaction, storage_parent, storage_name
            )
            if source_fd is None:
                source_fd = -1

        storage_meta = named(storage_parent, storage_name)
        pairing_meta = named(pairing_parent, pairing_name)
        if storage_meta is not None and not stat.S_ISDIR(storage_meta.st_mode):
            raise ResetError("storage")
        inferred = set()
        if source_fd >= 0:
            inferred.add(int(os.fstat(source_fd).st_uid))
        elif storage_meta is not None and stat.S_ISDIR(storage_meta.st_mode):
            inferred.add(int(storage_meta.st_uid))
        if pairing_meta is not None and stat.S_ISREG(pairing_meta.st_mode):
            inferred.add(int(pairing_meta.st_uid))
        if requested_uid is None:
            if any(value <= 0 for value in inferred) or len(inferred) > 1:
                raise ResetError("owner")
            uid = next(iter(inferred)) if inferred else None
        else:
            uid = requested_uid
            if inferred and inferred != {uid}:
                raise ResetError("owner")

        if transaction is None and storage_meta is not None:
            if uid is None:
                raise ResetError("owner")
            source_fd = open_storage_root(storage_parent, storage_name)
            if source_fd is None:
                raise ResetError("storage")
            source_location = "parent"
        if source_fd >= 0:
            if uid is None:
                raise ResetError("owner")
            verify_storage_owner(source_fd, uid, gid)
            scan_mount_tree(source_fd, deadline)
        if pairing_meta is not None:
            if uid is None:
                raise ResetError("pairing")
            pairing = bind_removable(
                pairing_parent, pairing_name, uid, gid, True, "pairing", True
            )
        if pairing is not None:
            verify_removable(pairing)
        elif named(pairing_parent, pairing_name) is not None:
            raise ResetError("pairing")

        check_deadline(deadline)
        if transaction is None and source_fd >= 0:
            transaction = create_transaction(
                storage_parent, source_fd, storage_name, deadline
            )
            quarantined = True
        elif transaction is not None:
            quarantined = True

        if transaction is not None and transaction["phase"] == "active" \
                and source_fd >= 0 and source_location == "parent":
            quarantine_storage(
                storage_parent,
                storage_name,
                source_fd,
                transaction["qfd"],
                deadline,
            )
            source_location = "quarantine"
        needs_replacement = (
            transaction is not None
            or load_state == "loaded"
        )
        if needs_replacement:
            if uid is None:
                raise ResetError("owner")
            replacement_root_fd = ensure_replacement(
                storage_parent,
                storage_name,
                uid,
                gid,
                transaction is not None,
                deadline,
            )
        elif named(storage_parent, storage_name) is not None:
            raise ResetError("storage")

        if transaction is not None and transaction["phase"] == "active":
            qfd = transaction["qfd"]
            if source_fd >= 0:
                if source_location != "quarantine":
                    raise ResetError("collision")
                clear_private_tree(source_fd, mount_id(source_fd), deadline)
                if os.listdir(source_fd):
                    raise ResetError("clear")
                moved = os.stat("storage", dir_fd=qfd, follow_symlinks=False)
                opened = os.fstat(source_fd)
                if ((moved.st_dev, moved.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise ResetError("collision")
                os.rmdir("storage", dir_fd=qfd)
                os.fsync(qfd)
                os.close(source_fd)
                source_fd = -1
            if set(os.listdir(qfd)) != {MARKER_NAME}:
                raise ResetError("collision")

        check_deadline(deadline)
        if pairing is not None:
            unlink_removable(pairing)
        elif named(pairing_parent, pairing_name) is not None:
            raise ResetError("pairing")

        if transaction is not None:
            if transaction["phase"] == "active":
                move_marker_to_receipt(storage_parent, transaction, deadline)
            finish_transaction_receipt(storage_parent, transaction, deadline)
        return quarantined
    finally:
        if pairing is not None:
            os.close(pairing["fd"])
        if replacement_root_fd >= 0:
            os.close(replacement_root_fd)
        if source_fd >= 0:
            os.close(source_fd)
        if transaction is not None:
            marker = transaction.get("marker")
            if isinstance(marker, dict) and marker.get("fd", -1) >= 0:
                os.close(marker["fd"])
                marker["fd"] = -1
            qfd = transaction.get("qfd")
            if isinstance(qfd, int) and qfd >= 0:
                os.close(qfd)
                transaction["qfd"] = None
        os.close(pairing_parent)
        os.close(storage_parent)

try:
    quarantined = reset()
    if quarantined:
        print("MATTER_RESET_STORAGE_QUARANTINED")
except ResetError as exc:
    token = str(exc)
    if token in {"entries", "depth"}:
        code = "STORAGE_LIMIT"
    elif token in {"storage", "root-swap", "tree", "entry", "owner", "user",
                   "group", "flags", "path", "parent", "mount"}:
        code = "STORAGE_BINDING"
    else:
        code = {"deadline":"STORAGE_TIMEOUT", "collision":"QUARANTINE_COLLISION",
                "foreign-mount":"STORAGE_FOREIGN_MOUNT", "hardlink":"STORAGE_HARDLINK",
                "pairing-hardlink":"PAIRING_HARDLINK", "clear":"STORAGE_CLEAR",
                "pairing-clear":"PAIRING_CLEAR", "pairing":"PAIRING_BINDING"}.get(
                    token, "INTERNAL")
    print(f"MATTER_RESET_ERROR_{code}", file=sys.stderr)
    raise SystemExit(1)
except (KeyError, OSError, TypeError, ValueError):
    print("MATTER_RESET_ERROR_INTERNAL", file=sys.stderr)
    raise SystemExit(1)
# E3DC_MATTER_RESET_PYTHON_END
PY
    then
        return 1
    fi
    if [[ "$load_state" == "not-found" ]]; then
        printf 'MATTER_RESET_OK_UNIT_MISSING\n'
        return 0
    fi
    if (( restart_allowed == 0 )); then
        printf 'MATTER_RESET_OK_LEFT_INACTIVE\n'
        return 0
    fi
    if ! config_state="$(matter_config_state 2>/dev/null)"; then
        matter_reset_error CONFIG_BINDING
        return 1
    fi
    if [[ "$config_state" != "enabled" ]]; then
        printf 'MATTER_RESET_OK_LEFT_INACTIVE\n'
        return 0
    fi
    if ! load_state="$(systemctl_value LoadState 2>/dev/null)"; then
        matter_reset_error UNIT_QUERY
        return 1
    fi
    if [[ "$load_state" == "not-found" ]]; then
        printf 'MATTER_RESET_OK_LEFT_INACTIVE\n'
        return 0
    fi
    if [[ "$load_state" != "loaded" ]] \
        || ! unit_file_state="$(systemctl_value UnitFileState 2>/dev/null)" \
        || ! active_state="$(systemctl_value ActiveState 2>/dev/null)" \
        || ! sub_state="$(systemctl_value SubState 2>/dev/null)" \
        || ! current_unit_user="$(systemctl_value User 2>/dev/null)" \
        || ! current_unit_group="$(systemctl_value Group 2>/dev/null)"; then
        matter_reset_error UNIT_CONTRACT
        return 1
    fi
    case "$unit_file_state" in
        enabled|enabled-runtime) ;;
        disabled|static|indirect|generated|transient|linked|linked-runtime|alias|masked|masked-runtime)
            printf 'MATTER_RESET_OK_LEFT_INACTIVE\n'
            return 0 ;;
        *) matter_reset_error UNIT_STATE; return 1 ;;
    esac
    if [[ "$active_state:$sub_state" != "inactive:dead" ]]; then
        matter_reset_error UNIT_STATE
        return 1
    fi
    if [[ "$current_unit_user:$current_unit_group" != "$unit_user:$unit_group" ]]; then
        matter_reset_error UNIT_OWNER
        return 1
    fi
    if ! restart_matter_service; then
        return 1
    fi
    printf 'MATTER_RESET_OK_STARTED\n'
}

if [[ "$ACTION" == "$MATTER_RESET_ACTION" ]]; then
    if [[ ! -x "$FLOCK" || ! -x "$MKDIR" || ! -x "$STAT" ]]; then
        printf 'Die fest gebundene Matter-Update-Sperre ist nicht verfügbar.\n' >&2
        exit 126
    fi
    if ! acquire_matter_update_lock; then
        matter_reset_error "$MATTER_UPDATE_LOCK_ERROR"
        exit 1
    fi
fi
if [[ "$SERVICE" == "$MATTER_SERVICE" ]]; then
    if [[ ! -x "$FLOCK" || ! -x "$INSTALL" || ! -x "$STAT" ]]; then
        printf 'Die fest gebundene Matter-Service-Sperre ist nicht verfügbar.\n' >&2
        exit 126
    fi
    if ! acquire_matter_lock; then
        matter_reset_error LOCK
        exit 1
    fi
fi
if [[ "$ACTION" == "$MATTER_RESET_ACTION" ]]; then
    reset_matter_pairing
    exit $?
fi

printf 'Führe systemctl %s %s aus...\n' "$ACTION" "$SERVICE"
if [ "$ACTION" == "status" ]; then
    exec "$ENV" -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C \
        "$SYSTEMCTL" --no-pager status -- "$SERVICE"
fi
if [ "$ACTION" == "restart" ] || [ "$ACTION" == "start" ]; then
    "$ENV" -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C \
        "$SYSTEMCTL" reset-failed -- "$SERVICE" 2>/dev/null || true
fi
exec "$ENV" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    "$SYSTEMCTL" "$ACTION" -- "$SERVICE"
