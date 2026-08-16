#!/bin/bash
# E3DC-Control Web-Update-Launcher
#
# Diese Datei ist eine Installationsvorlage. Der Web-Installer bindet
# Installationspfad und -benutzer ein und projiziert das Ergebnis root-eigen
# nach /usr/local/sbin. Die WebUI darf den installierten Launcher nur ohne
# Argumente starten. Der eigentliche Updateprozess läuft anschließend als
# isolierter systemd-Job aus einem versiegelten Git-Snapshot.

set -euo pipefail
umask 027

readonly INSTALL_ROOT="@E3DC_INSTALL_ROOT@"
readonly INSTALL_USER="@E3DC_INSTALL_USER@"
readonly INSTALLED_RELEASE_COMMIT="@E3DC_RELEASE_COMMIT@"
readonly LAUNCHER="/usr/local/sbin/e3dc-web-update-launcher"
readonly UNIT="e3dc-web-update.service"
readonly RUNTIME_DIR="/run/e3dc-web-update"
readonly LOG_DIR="/var/log/e3dc-control"
readonly LOG_FILE="${LOG_DIR}/web-update.log"
readonly PID_FILE="${RUNTIME_DIR}/pid"
readonly STATUS_FILE="${RUNTIME_DIR}/status"
readonly LOCK_FILE="${RUNTIME_DIR}/lock"
readonly GIT="/usr/bin/git"
readonly PYTHON="/usr/bin/python3"
readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SYSTEMD_RUN="/usr/bin/systemd-run"
readonly FLOCK="/usr/bin/flock"
readonly ENV="/usr/bin/env"
readonly SUDO="/usr/bin/sudo"
readonly TIMEOUT="/usr/bin/timeout"
readonly REMOTE_URL="https://github.com/A9xxx/Install-E3DC-Control.git"
readonly SNAPSHOT_MAX_FILES=4096
readonly SNAPSHOT_MAX_FILE_BYTES=$((8 * 1024 * 1024))
readonly SNAPSHOT_MAX_TOTAL_BYTES=$((128 * 1024 * 1024))
readonly SNAPSHOT_MAX_REPOSITORY_BYTES=$((256 * 1024 * 1024))
readonly SNAPSHOT_MAX_PATH_BYTES=512
readonly SNAPSHOT_PATH_PATTERN='^[A-Za-z0-9._/@+ -]+$'
WORKER_SNAPSHOT=""

git_local_read() {
    (
        cd /
        "$SUDO" -n -H -u "$INSTALL_USER" -- "$ENV" -i \
            PATH=/usr/bin:/bin \
            HOME=/nonexistent \
            XDG_CONFIG_HOME=/nonexistent \
            LANG=C \
            LC_ALL=C \
            GIT_CONFIG_NOSYSTEM=1 \
            GIT_CONFIG_SYSTEM=/dev/null \
            GIT_CONFIG_GLOBAL=/dev/null \
            GIT_ATTR_NOSYSTEM=1 \
            GIT_NO_REPLACE_OBJECTS=1 \
            GIT_OPTIONAL_LOCKS=0 \
            GIT_TERMINAL_PROMPT=0 \
            GIT_ASKPASS=/bin/false \
            SSH_ASKPASS=/bin/false \
            GIT_ALLOW_PROTOCOL=https \
            GIT_LITERAL_PATHSPECS=1 \
            GIT_CEILING_DIRECTORIES=/ \
            "$TIMEOUT" 60 \
            "$GIT" --no-replace-objects \
                -c core.hooksPath=/dev/null \
                -c core.fsmonitor=false \
                -c core.untrackedCache=false \
                -c core.attributesFile=/dev/null \
                -c fsck.skipList=/dev/null \
                -c protocol.ext.allow=never \
                -c protocol.file.allow=never \
                --git-dir="${INSTALL_ROOT}/.git" \
                --work-tree="$INSTALL_ROOT" \
                "$@"
    )
}

git_remote_tag() {
    local tag_ref="$1"
    (
        cd /
        "$SUDO" -n -H -u "$INSTALL_USER" -- "$ENV" -i \
            PATH=/usr/bin:/bin \
            HOME=/nonexistent \
            XDG_CONFIG_HOME=/nonexistent \
            LANG=C \
            LC_ALL=C \
            GIT_CONFIG_NOSYSTEM=1 \
            GIT_CONFIG_SYSTEM=/dev/null \
            GIT_CONFIG_GLOBAL=/dev/null \
            GIT_ATTR_NOSYSTEM=1 \
            GIT_NO_REPLACE_OBJECTS=1 \
            GIT_OPTIONAL_LOCKS=0 \
            GIT_TERMINAL_PROMPT=0 \
            GIT_ASKPASS=/bin/false \
            SSH_ASKPASS=/bin/false \
            GIT_ALLOW_PROTOCOL=https \
            GIT_CEILING_DIRECTORIES=/ \
            "$TIMEOUT" 30 \
            "$GIT" --no-replace-objects \
            -c credential.helper= \
            -c core.askPass=/bin/false \
            -c core.hooksPath=/dev/null \
            -c protocol.ext.allow=never \
            -c protocol.file.allow=never \
            ls-remote --exit-code --tags "$REMOTE_URL" "$tag_ref"
    )
}

git_snapshot_root() {
    local repository="$1"
    shift
    (
        cd /
        "$ENV" -i \
            PATH=/usr/bin:/bin \
            HOME=/nonexistent \
            XDG_CONFIG_HOME=/nonexistent \
            LANG=C \
            LC_ALL=C \
            GIT_CONFIG_NOSYSTEM=1 \
            GIT_CONFIG_SYSTEM=/dev/null \
            GIT_CONFIG_GLOBAL=/dev/null \
            GIT_ATTR_NOSYSTEM=1 \
            GIT_NO_REPLACE_OBJECTS=1 \
            GIT_OPTIONAL_LOCKS=0 \
            GIT_TERMINAL_PROMPT=0 \
            GIT_ASKPASS=/bin/false \
            SSH_ASKPASS=/bin/false \
            GIT_ALLOW_PROTOCOL=https \
            GIT_LITERAL_PATHSPECS=1 \
            GIT_CEILING_DIRECTORIES=/ \
            "$TIMEOUT" 120 \
            "$GIT" --no-replace-objects \
                -c credential.helper= \
                -c core.askPass=/bin/false \
                -c core.hooksPath=/dev/null \
                -c core.fsmonitor=false \
                -c core.untrackedCache=false \
                -c core.attributesFile=/dev/null \
                -c core.useReplaceRefs=false \
                -c fsck.skipList=/dev/null \
                -c fetch.fsckObjects=true \
                -c transfer.fsckObjects=true \
                -c protocol.ext.allow=never \
                -c protocol.file.allow=never \
                --git-dir="$repository" \
                "$@"
    )
}

assert_no_git_rewrites() {
    local replace_refs grafts
    grafts="${INSTALL_ROOT}/.git/info/grafts"
    [[ ! -e "$grafts" && ! -L "$grafts" ]] \
        || fail "Repository enthält nicht freigegebene Git-Grafts"
    replace_refs="$(git_local_read for-each-ref --format='%(refname)' refs/replace)" \
        || fail "Git-Replace-Referenzen konnten nicht geprüft werden"
    [[ -z "$replace_refs" ]] \
        || fail "Repository enthält nicht freigegebene Replace Objects"
}

fail() {
    printf 'Web-Update-Launcher: %s\n' "$1" >&2
    exit "${2:-1}"
}

require_root_path() {
    local path="$1"
    local expected_mode="$2"
    local expected_group="$3"
    local owner group mode kind
    IFS=' ' read -r owner group mode kind < <(/usr/bin/stat -c '%u %g %a %F' -- "$path") \
        || fail "Pfadmetadaten fehlen: ${path}" 126
    [[ "$owner" == "0" && "$group" == "$expected_group" ]] \
        || fail "Pfad ist nicht root-eigen: ${path}" 126
    [[ "$mode" == "$expected_mode" ]] \
        || fail "Pfadmodus weicht ab: ${path}" 126
    [[ "$kind" == "directory" || "$kind" == "regular file" ]] \
        || fail "Pfadtyp ist nicht zulässig: ${path}" 126
}

prepare_runtime_paths() {
    local www_data_gid
    www_data_gid="$(/usr/bin/getent group www-data | /usr/bin/cut -d: -f3)"
    [[ "$www_data_gid" =~ ^[0-9]+$ ]] || fail "www-data-Gruppe fehlt" 126
    [[ ! -L "$RUNTIME_DIR" && ! -L "$LOG_DIR" ]] \
        || fail "Runtime- oder Logpfad ist ein Symlink" 126
    /usr/bin/install -d -o root -g www-data -m 0750 -- "$RUNTIME_DIR"
    /usr/bin/install -d -o root -g root -m 0755 -- "$LOG_DIR"
    require_root_path /run 755 0
    require_root_path "$RUNTIME_DIR" 750 "$www_data_gid"
    require_root_path /var/log 755 0
    require_root_path "$LOG_DIR" 755 0
}

write_runtime_value() {
    local target="$1"
    local value="$2"
    local temporary
    temporary="$(/usr/bin/mktemp "${RUNTIME_DIR}/.$(/usr/bin/basename "$target").XXXXXX")"
    printf '%s\n' "$value" > "$temporary"
    /usr/bin/chown root:www-data -- "$temporary"
    /usr/bin/chmod 0640 -- "$temporary"
    /usr/bin/mv -fT -- "$temporary" "$target"
}

prepare_log() {
    local metadata
    if [[ -e "$LOG_FILE" || -L "$LOG_FILE" ]]; then
        [[ ! -L "$LOG_FILE" ]] || fail "Updateprotokoll ist ein Symlink" 126
        metadata="$(/usr/bin/stat -c '%u %g %a %F %h' -- "$LOG_FILE")" \
            || fail "Updateprotokoll ist nicht prüfbar" 126
        [[ "$metadata" == "0 $(/usr/bin/getent group www-data | /usr/bin/cut -d: -f3) 640 regular file 1" ]] \
            || fail "Updateprotokoll besitzt unzulässige Metadaten" 126
    else
        /usr/bin/install -o root -g www-data -m 0640 /dev/null "$LOG_FILE"
    fi
    : > "$LOG_FILE"
    printf '=== E3DC-Control Web-Update %s ===\n' "$(/usr/bin/date --iso-8601=seconds)" >> "$LOG_FILE"
}

validate_product_identity() {
    local canonical_root canonical_git git_dir account_home owner mode origin
    local head rebound version tag_ref peeled_sha object_format tracked_state

    canonical_root="$(/usr/bin/readlink -f -- "$INSTALL_ROOT")" \
        || fail "Installationspfad konnte nicht aufgelöst werden"
    [[ ! -L "$INSTALL_ROOT" && "$canonical_root" == "$INSTALL_ROOT" ]] \
        || fail "Installationspfad ist nicht kanonisch"
    git_dir="${INSTALL_ROOT}/.git"
    canonical_git="$(/usr/bin/readlink -f -- "$git_dir")" \
        || fail "Git-Verzeichnis konnte nicht aufgelöst werden"
    [[ -d "$git_dir" && ! -L "$git_dir" && "$canonical_git" == "$git_dir" ]] \
        || fail "Git-Verzeichnis ist nicht kanonisch gebunden"
    account_home="$(/usr/bin/getent passwd "$INSTALL_USER" | /usr/bin/cut -d: -f6)"
    [[ -n "$account_home" && "$INSTALL_USER" != "root" && "$INSTALL_USER" != "www-data" ]] \
        || fail "Installationsbenutzer ist nicht zulässig"
    IFS=' ' read -r owner mode < <(/usr/bin/stat -Lc '%U %a' -- "$INSTALL_ROOT") \
        || fail "Installationspfad ist nicht prüfbar"
    [[ "$owner" == "$INSTALL_USER" && "$mode" =~ ^[0-7]{3,4}$ ]] \
        || fail "Installationspfad ist nicht an den Installationsbenutzer gebunden"
    (( (8#$mode & 8#022) == 0 )) \
        || fail "Installationspfad ist für Gruppe oder Andere schreibbar"
    [[ -f "$INSTALL_ROOT/VERSION" && -f "$INSTALL_ROOT/UPDATE_POLICY.json" \
        && -f "$INSTALL_ROOT/installer_main.py" && -d "$INSTALL_ROOT/.git" ]] \
        || fail "Installationspfad besitzt keinen vollständigen Releasevertrag"

    for binary in "$GIT" "$PYTHON" "$SYSTEMCTL" "$SYSTEMD_RUN" "$FLOCK" "$ENV" "$SUDO" "$TIMEOUT"; do
        [[ -x "$binary" ]] || fail "Fest gebundenes Systemprogramm fehlt: ${binary}" 126
    done

    origin="$(git_local_read config --local --no-includes --get-all remote.origin.url)" \
        || fail "Lokaler Repository-Ursprung konnte nicht geprüft werden"
    [[ -n "$origin" && "$origin" != *$'\n'* ]] \
        || fail "Lokaler Repository-Ursprung ist nicht eindeutig"
    [[ "$origin" == "$REMOTE_URL" \
        || "$origin" == "${REMOTE_URL%.git}" ]] \
        || fail "Repository-Ursprung ist nicht freigegeben"
    object_format="$(git_local_read rev-parse --show-object-format)" \
        || fail "Git-Objektformat konnte nicht gebunden werden"
    [[ "$object_format" == "sha1" ]] \
        || fail "Repository verwendet kein freigegebenes SHA-1-Objektformat"
    assert_no_git_rewrites
    tracked_state="$(git_local_read status \
        --porcelain=v1 --untracked-files=no --ignore-submodules=none)" \
        || fail "Lokaler Änderungszustand konnte nicht geprüft werden"
    [[ -z "$tracked_state" ]] \
        || fail "Repository enthält lokale Änderungen an Produktdateien"
    head="$(git_local_read rev-parse --verify 'HEAD^{commit}')"
    [[ "$head" =~ ^[0-9a-f]{40}$ ]] || fail "Ausgangscommit ist ungültig"
    [[ "$INSTALLED_RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ \
        && "$head" == "$INSTALLED_RELEASE_COMMIT" ]] \
        || fail "Ausgangscommit weicht vom root-gebundenen Launcher-Release ab"

    version="$(/usr/bin/tr -d '\r\n' < "$INSTALL_ROOT/VERSION")"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.-]*$ ]] \
        || fail "Ausgangsversion ist ungültig"
    tag_ref="refs/tags/v${version}^{}"
    peeled_sha="$(git_remote_tag "$tag_ref" | /usr/bin/cut -f1)" \
        || fail "Veröffentlichter Ausgangstag konnte nicht gebunden werden"
    [[ "$peeled_sha" == "$head" ]] \
        || fail "Ausgangscommit stimmt nicht mit dem veröffentlichten Tag überein"
    assert_no_git_rewrites
    rebound="$(git_local_read rev-parse --verify 'HEAD^{commit}')"
    [[ "$rebound" == "$head" ]] || fail "Ausgangscommit driftete während der Bindung"
    printf '%s\t%s\n' "$head" "$version"
}

create_execution_snapshot() {
    local head="$1"
    local version="$2"
    local snapshot="$3"
    local entry prefix relative_path casefold_path mode type object_id extra component
    local tree_listing object_ids object_info object_batch manifest repository template
    local fetched_head object_format repository_bytes
    local count=0 index=0 total=0 object_size checked_id checked_type checked_size
    local -a paths=() modes=() object_ids_by_index=() components=()
    local -A seen_paths=() seen_casefold_paths=()

    tree_listing="${snapshot}/tree"
    object_ids="${snapshot}/objects"
    object_info="${snapshot}/object-info"
    object_batch="${snapshot}/object-batch"
    manifest="${snapshot}/manifest"
    repository="${snapshot}/repository"
    template="${snapshot}/template"
    assert_no_git_rewrites
    /usr/bin/mkdir -m 0700 -- "$repository"
    /usr/bin/mkdir -m 0700 -- "$template"
    git_snapshot_root "$repository" init \
        --bare --template="$template" --object-format=sha1 \
        --initial-branch=e3dc-snapshot >/dev/null \
        || fail "Neutrales Snapshot-Repository konnte nicht erstellt werden"
    /usr/bin/rmdir -- "$template"
    /usr/bin/chown root:root -- "$repository"
    /usr/bin/chmod 0700 -- "$repository"
    git_snapshot_root "$repository" fetch \
        --force \
        --no-tags \
        --no-recurse-submodules \
        --no-write-fetch-head \
        --depth=1 \
        "$REMOTE_URL" \
        "refs/tags/v${version}:refs/tags/e3dc-bound" >/dev/null \
        || fail "Gebundener Release-Tag konnte nicht neutral abgerufen werden"
    object_format="$(git_snapshot_root "$repository" rev-parse --show-object-format)" \
        || fail "Objektformat des Snapshot-Repositorys konnte nicht geprüft werden"
    [[ "$object_format" == "sha1" ]] \
        || fail "Snapshot-Repository verwendet kein freigegebenes SHA-1-Objektformat"
    fetched_head="$(git_snapshot_root "$repository" rev-parse --verify 'refs/tags/e3dc-bound^{commit}')" \
        || fail "Abgerufener Release-Tag besitzt keinen gebundenen Commit"
    [[ "$fetched_head" == "$head" ]] \
        || fail "Neutral abgerufener Release-Tag stimmt nicht mit dem Ausgangscommit überein"
    repository_bytes="$(/usr/bin/du -sb -- "$repository" | /usr/bin/cut -f1)" \
        || fail "Größe des Snapshot-Repositorys konnte nicht geprüft werden"
    [[ "$repository_bytes" =~ ^[0-9]+$ \
        && "$repository_bytes" -le $SNAPSHOT_MAX_REPOSITORY_BYTES ]] \
        || fail "Snapshot-Repository überschreitet die feste Gesamtgröße"
    git_snapshot_root "$repository" fsck --strict --no-dangling "$head" >/dev/null \
        || fail "Neutral abgerufener Commit besitzt keine hashkonsistente Objektkette"
    if ! git_snapshot_root "$repository" ls-tree -r -z --full-tree "$head" -- > "$tree_listing"; then
        fail "Releasebaum konnte nicht aus dem gebundenen Commit gelesen werden"
    fi
    /usr/bin/chown root:root -- "$tree_listing"
    /usr/bin/chmod 0600 -- "$tree_listing"
    : > "$object_ids"
    /usr/bin/chown root:root -- "$object_ids"
    /usr/bin/chmod 0600 -- "$object_ids"
    while IFS= read -r -d '' entry; do
        [[ "$entry" == *$'\t'* ]] \
            || fail "Releasebaum enthält einen unlesbaren Git-Eintrag"
        prefix="${entry%%$'\t'*}"
        relative_path="${entry#*$'\t'}"
        casefold_path="${relative_path,,}"
        IFS=' ' read -r mode type object_id extra <<< "$prefix"
        [[ -z "${extra:-}" && "$type" == "blob" \
            && ( "$mode" == "100644" || "$mode" == "100755" ) \
            && "$object_id" =~ ^[0-9a-f]{40}$ ]] \
            || fail "Releasebaum enthält einen unzulässigen Objekt- oder Linktyp"
        [[ -n "$relative_path" \
            && ${#relative_path} -le $SNAPSHOT_MAX_PATH_BYTES \
            && "$relative_path" =~ $SNAPSHOT_PATH_PATTERN \
            && "$relative_path" != /* \
            && "$relative_path" != */ \
            && "$relative_path" != *//* \
            && -z "${seen_paths[$relative_path]+x}" \
            && -z "${seen_casefold_paths[$casefold_path]+x}" ]] \
            || fail "Releasebaum enthält einen unzulässigen oder doppelten Pfad"
        IFS='/' read -r -a components <<< "$relative_path"
        for component in "${components[@]}"; do
            [[ -n "$component" && "$component" != "." && "$component" != ".." ]] \
                || fail "Releasebaum enthält eine unzulässige Pfadkomponente"
        done
        (( count < SNAPSHOT_MAX_FILES )) \
            || fail "Releasebaum überschreitet die feste Dateianzahl"
        seen_paths["$relative_path"]=1
        seen_casefold_paths["$casefold_path"]=1
        paths[count]="$relative_path"
        modes[count]="$mode"
        object_ids_by_index[count]="$object_id"
        printf '%s\n' "$object_id" >> "$object_ids"
        count=$((count + 1))
    done < "$tree_listing"
    (( count > 0 )) || fail "Releasebaum enthält keine regulären Produktdateien"

    if ! git_snapshot_root "$repository" cat-file \
        --batch-check='%(objectname) %(objecttype) %(objectsize)' \
        < "$object_ids" > "$object_info"; then
        fail "Blobgrößen konnten nicht aus dem gebundenen Commit gelesen werden"
    fi
    /usr/bin/chown root:root -- "$object_info"
    /usr/bin/chmod 0600 -- "$object_info"
    : > "$manifest"
    /usr/bin/chown root:root -- "$manifest"
    /usr/bin/chmod 0600 -- "$manifest"
    while IFS=' ' read -r checked_id checked_type checked_size extra; do
        (( index < count )) || fail "Git lieferte überzählige Blobmetadaten"
        [[ -z "${extra:-}" \
            && "$checked_id" == "${object_ids_by_index[index]}" \
            && "$checked_type" == "blob" \
            && "$checked_size" =~ ^[0-9]+$ \
            && "$checked_size" -le $SNAPSHOT_MAX_FILE_BYTES ]] \
            || fail "Git lieferte unzulässige oder widersprüchliche Blobmetadaten"
        object_size=$((10#$checked_size))
        total=$((total + object_size))
        (( total <= SNAPSHOT_MAX_TOTAL_BYTES )) \
            || fail "Releasebaum überschreitet die feste Gesamtgröße"
        printf '%s\t%s\t%s\t%s\n' \
            "${modes[index]}" "$checked_id" "$object_size" "${paths[index]}" \
            >> "$manifest"
        index=$((index + 1))
    done < "$object_info"
    (( index == count )) || fail "Git lieferte unvollständige Blobmetadaten"

    if ! git_snapshot_root "$repository" cat-file --batch < "$object_ids" > "$object_batch"; then
        fail "Blobs konnten nicht aus dem gebundenen Commit gelesen werden"
    fi
    /usr/bin/chown root:root -- "$object_batch"
    /usr/bin/chmod 0600 -- "$object_batch"
    /usr/bin/mkdir -m 0700 -- "$snapshot/root"
    "$ENV" -i \
        PATH=/usr/bin:/bin \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        PYTHONNOUSERSITE=1 \
        "$PYTHON" -I -B - "$manifest" "$object_batch" "$snapshot/root" <<'PY'
import os
import hashlib
import stat
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
batch_path = Path(sys.argv[2])
snapshot_root = Path(sys.argv[3])

root_meta = snapshot_root.lstat()
if (
    snapshot_root.is_symlink()
    or not stat.S_ISDIR(root_meta.st_mode)
    or root_meta.st_uid != 0
    or root_meta.st_gid != 0
    or stat.S_IMODE(root_meta.st_mode) != 0o700
):
    raise SystemExit("Snapshotwurzel ist nicht exklusiv root-gebunden")

records = []
seen = set()
for raw_line in manifest_path.read_bytes().splitlines():
    fields = raw_line.split(b"\t", 3)
    if len(fields) != 4:
        raise SystemExit("Snapshotmanifest besitzt kein eindeutiges Format")
    raw_mode, raw_oid, raw_size, raw_path = fields
    try:
        relative = raw_path.decode("ascii")
        size = int(raw_size.decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError):
        raise SystemExit("Snapshotmanifest besitzt ungültige Textfelder")
    if (
        raw_mode not in {b"100644", b"100755"}
        or len(raw_oid) != 40
        or any(byte not in b"0123456789abcdef" for byte in raw_oid)
        or size < 0
        or relative in seen
        or not relative
    ):
        raise SystemExit("Snapshotmanifest besitzt unzulässige Blobfelder")
    seen.add(relative)
    records.append((raw_mode, raw_oid, size, relative))

directory_flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
file_flags = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

root_fd = os.open(snapshot_root, directory_flags)
try:
    with batch_path.open("rb", buffering=0) as batch:
        for raw_mode, raw_oid, size, relative in records:
            expected_header = raw_oid + b" blob " + str(size).encode("ascii") + b"\n"
            header = batch.readline(256)
            if header != expected_header:
                raise SystemExit("Git-Batchantwort widerspricht dem Blobmanifest")

            components = relative.split("/")
            parent_fd = os.dup(root_fd)
            try:
                for component in components[:-1]:
                    try:
                        os.mkdir(component, 0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                    metadata = os.fstat(next_fd)
                    if (
                        not stat.S_ISDIR(metadata.st_mode)
                        or metadata.st_uid != 0
                        or metadata.st_gid != 0
                        or stat.S_IMODE(metadata.st_mode) != 0o700
                    ):
                        os.close(next_fd)
                        raise SystemExit("Snapshotpfad enthält ein fremdes Verzeichnis")
                    os.close(parent_fd)
                    parent_fd = next_fd

                descriptor = os.open(components[-1], file_flags, 0o600, dir_fd=parent_fd)
                try:
                    remaining = size
                    digest = hashlib.sha1()
                    digest.update(b"blob " + str(size).encode("ascii") + b"\0")
                    while remaining:
                        chunk = batch.read(min(65536, remaining))
                        if not chunk:
                            raise SystemExit("Git-Batchantwort endet innerhalb eines Blobs")
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise SystemExit("Snapshotblob konnte nicht vollständig geschrieben werden")
                            view = view[written:]
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if batch.read(1) != b"\n":
                        raise SystemExit("Git-Batchantwort besitzt keinen eindeutigen Blobabschluss")
                    if digest.hexdigest().encode("ascii") != raw_oid:
                        raise SystemExit("Materialisierter Snapshotblob widerspricht seiner Git-OID")
                    final_mode = 0o555 if raw_mode == b"100755" else 0o444
                    os.fchmod(descriptor, final_mode)
                    os.fsync(descriptor)
                    metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or metadata.st_uid != 0
                        or metadata.st_gid != 0
                        or stat.S_IMODE(metadata.st_mode) != final_mode
                        or metadata.st_size != size
                    ):
                        raise SystemExit("Materialisierter Snapshotblob besitzt unzulässige Metadaten")
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_fd)
        if batch.read(1):
            raise SystemExit("Git-Batchantwort enthält überzählige Daten")
finally:
    os.close(root_fd)

for directory, dirnames, _filenames in os.walk(snapshot_root, topdown=False, followlinks=False):
    for dirname in dirnames:
        path = Path(directory) / dirname
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit("Snapshot enthält nach Materialisierung einen fremden Pfadtyp")
        path.chmod(0o555)
    Path(directory).chmod(0o555)
PY
    /usr/bin/unlink -- "$tree_listing"
    /usr/bin/unlink -- "$object_ids"
    /usr/bin/unlink -- "$object_info"
    /usr/bin/unlink -- "$object_batch"
    /usr/bin/unlink -- "$manifest"
    git_snapshot_root "$repository" fsck --strict --no-dangling "$head" >/dev/null \
        || fail "Neutrale Git-Objektkette driftete während der Snapshot-Erstellung"
    /usr/bin/find "$repository" -depth -delete
    assert_no_git_rewrites
    [[ -f "$snapshot/root/installer_main.py" && -f "$snapshot/root/Installer/update.py" ]] \
        || fail "Versiegelter Ausführungssnapshot ist unvollständig"
    [[ "$(/usr/bin/tr -d '\r\n' < "$snapshot/root/VERSION")" == "$version" ]] \
        || fail "Versiegelter Ausführungssnapshot besitzt eine widersprüchliche Version"
}

run_worker() {
    local identity head version extra rebound_state result=1
    [[ "${E3DC_WEB_UPDATE_WORKER:-}" == "1" && -n "${INVOCATION_ID:-}" ]] \
        || fail "Worker besitzt keinen systemd-Ausführungsvertrag" 126
    [[ -z "${SUDO_USER:-}" ]] || fail "Worker übernimmt keinen sudo-Aufrufer" 126

    prepare_runtime_paths
    exec 9>"$LOCK_FILE"
    $FLOCK -n 9 || fail "Ein Web-Update läuft bereits" 75
    WORKER_SNAPSHOT=""
    cleanup() {
        local exit_code=$?
        local snapshot_path="${WORKER_SNAPSHOT:-}"
        set +e
        if [[ -n "$snapshot_path" && -d "$snapshot_path" \
            && "$snapshot_path" == /run/e3dc-web-update.snapshot.* ]]; then
            /usr/bin/find "$snapshot_path" -depth -delete
        fi
        write_runtime_value "$STATUS_FILE" "$exit_code"
        /usr/bin/unlink "$PID_FILE" 2>/dev/null || true
        return "$exit_code"
    }
    trap cleanup EXIT
    prepare_log
    exec >> "$LOG_FILE" 2>&1
    write_runtime_value "$PID_FILE" "$$"
    write_runtime_value "$STATUS_FILE" "running"
    WORKER_SNAPSHOT="$(/usr/bin/mktemp -d /run/e3dc-web-update.snapshot.XXXXXX)"
    /usr/bin/chown root:root -- "$WORKER_SNAPSHOT"
    /usr/bin/chmod 0700 -- "$WORKER_SNAPSHOT"

    identity="$(validate_product_identity)"
    IFS=$'\t' read -r head version extra <<< "$identity"
    [[ "$head" =~ ^[0-9a-f]{40}$ \
        && "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.-]*$ \
        && -z "${extra:-}" ]] \
        || fail "Gebundene Produktidentität besitzt kein eindeutiges Format"
    printf '[OK] Veröffentlichter Ausgangsstand gebunden: %s\n' "$head"
    create_execution_snapshot "$head" "$version" "$WORKER_SNAPSHOT"
    [[ "$(git_local_read rev-parse --verify 'HEAD^{commit}')" == "$head" ]] \
        || fail "Ausgangscommit driftete während der Snapshot-Erstellung"
    rebound_state="$(git_local_read status \
        --porcelain=v1 --untracked-files=no --ignore-submodules=none)" \
        || fail "Lokaler Änderungszustand konnte nicht erneut geprüft werden"
    [[ -z "$rebound_state" ]] \
        || fail "Repository driftete während der Snapshot-Erstellung"
    assert_no_git_rewrites
    printf '[OK] Root-eigener Ausführungssnapshot erstellt.\n'

    set +e
    $ENV -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        HOME="$(/usr/bin/getent passwd "$INSTALL_USER" | /usr/bin/cut -d: -f6)" \
        USER="$INSTALL_USER" \
        LOGNAME="$INSTALL_USER" \
        SUDO_USER="$INSTALL_USER" \
        E3DC_BOOTSTRAP_USER="$INSTALL_USER" \
        E3DC_INSTALL_ROOT="$INSTALL_ROOT" \
        PYTHONNOUSERSITE=1 \
        "$PYTHON" -I -B -u "$WORKER_SNAPSHOT/root/installer_main.py" --update-e3dc
    result=$?
    set -e
    if (( result == 0 )); then
        printf '[OK] Web-Update abgeschlossen.\n'
    else
        printf '[!] Web-Update fehlgeschlagen (Exit %d).\n' "$result"
    fi
    return "$result"
}

start_worker() {
    local launch_output launch_status
    [[ "${SUDO_USER:-}" == "www-data" ]] \
        || fail "Webstart ist ausschließlich für www-data freigegeben" 126
    (( $# == 0 )) || fail "Der Web-Update-Launcher akzeptiert keine Argumente" 64
    prepare_runtime_paths
    if $SYSTEMCTL --quiet is-active "$UNIT"; then
        printf 'Web-Update läuft bereits.\n'
        return 0
    fi
    prepare_log
    write_runtime_value "$STATUS_FILE" "launching"
    /usr/bin/unlink "$PID_FILE" 2>/dev/null || true
    $SYSTEMCTL reset-failed "$UNIT" 2>/dev/null || true
    set +e
    launch_output="$($SYSTEMD_RUN \
        --unit="${UNIT%.service}" \
        --collect \
        --no-block \
        --property=Type=exec \
        --property=User=root \
        --property=Group=root \
        --property=UMask=0027 \
        --property=TimeoutStartSec=infinity \
        --setenv=E3DC_WEB_UPDATE_WORKER=1 \
        --setenv=SUDO_USER= \
        "$LAUNCHER" --worker 2>&1)"
    launch_status=$?
    set -e
    if (( launch_status != 0 )); then
        printf '[!] Web-Update-Systemjob konnte nicht gestartet werden (Exit %d).\n%s\n' \
            "$launch_status" "$launch_output" >> "$LOG_FILE"
        write_runtime_value "$STATUS_FILE" "$launch_status"
        fail "Web-Update-Systemjob konnte nicht gestartet werden" "$launch_status"
    fi
    [[ -z "$launch_output" ]] || printf '%s\n' "$launch_output"
    printf 'Web-Update wurde als root-kontrollierter Systemjob gestartet.\n'
}

if (( EUID != 0 )); then
    fail "Launcher benötigt Root-Rechte" 77
fi

if [[ "${1:-}" == "--worker" ]]; then
    (( $# == 1 )) || fail "Worker akzeptiert keine weiteren Argumente" 64
    run_worker
else
    start_worker "$@"
fi
