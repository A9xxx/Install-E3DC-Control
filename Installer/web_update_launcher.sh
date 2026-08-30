#!/bin/bash
# E3DC-Control Web-/Konsolen-Update-Dispatcher
#
# Diese Installationsvorlage wird root-eigen nach /usr/local/sbin projiziert.
# Der eingebettete Installationspfad und -benutzer binden diesen Webaufruf an
# genau die Installation, aus der der Launcher projiziert wurde. Der
# installierte Produktbaum und seine Git-Metadaten sind keine Updatequelle: Der
# Systemjob lädt das neueste veröffentlichte Release genau einmal in ein
# privates Root-Verzeichnis; dessen Discovery bestätigt Pfad, Benutzer und
# Rolle. Eine globale Kandidatensuche bleibt Konsolenaufrufen ohne expliziten
# Pfad vorbehalten.

set -euo pipefail
umask 027

readonly INSTALL_ROOT=@E3DC_INSTALL_ROOT@
readonly INSTALL_USER=@E3DC_INSTALL_USER@
readonly DISPATCHER_CONTRACT="e3dc-download-bootstrap-v2"
readonly LAUNCHER="/usr/local/sbin/e3dc-web-update-launcher"
readonly UNIT="e3dc-web-update.service"
readonly RUNTIME_DIR="/run/e3dc-web-update"
readonly LOG_DIR="/var/log/e3dc-control"
readonly LOG_FILE="${LOG_DIR}/web-update.log"
readonly PID_FILE="${RUNTIME_DIR}/pid"
readonly STATUS_FILE="${RUNTIME_DIR}/status"
readonly LOCK_FILE="${RUNTIME_DIR}/lock"
readonly START_LOCK_FILE="${RUNTIME_DIR}/start.lock"
readonly REPOSITORY_URL="https://github.com/A9xxx/Install-E3DC-Control"
readonly GIT_URL="${REPOSITORY_URL}.git"
readonly LATEST_URL="${REPOSITORY_URL}/releases/latest"
readonly SYSTEMCTL="/usr/bin/systemctl"
readonly SYSTEMD_RUN="/usr/bin/systemd-run"
readonly FLOCK="/usr/bin/flock"
readonly CURL="/usr/bin/curl"
readonly GIT="/usr/bin/git"
readonly PYTHON="/usr/bin/python3"
readonly ENV="/usr/bin/env"
DOWNLOAD_DIR=""

fail() {
    local message="$1"
    local exit_code="${2:-1}"
    local explicit_solution="${3:-}"
    local solution
    if [[ -n "$explicit_solution" ]]; then
        solution="$explicit_solution"
    else
        case "$exit_code" in
            64)
                solution="Starte den installierten Web-Update-Dispatcher ohne zusätzliche Argumente."
                ;;
            75)
                solution="Warte auf den laufenden Updatejob und prüfe: systemctl status --no-pager ${UNIT}"
                ;;
            77)
                solution="Starte den Dispatcher über die Weboberfläche oder mit sudo."
                ;;
            126)
                solution="Repariere den root-eigenen Dispatcher einmalig mit dem aktuellen Community-Bootstrap und starte danach das Webupdate erneut."
                ;;
            *)
                solution="Prüfe die unmittelbar vorherige Ursache sowie ${LOG_FILE} und starte denselben Updatebefehl anschließend erneut."
                ;;
        esac
    fi
    printf '\n[ABBRUCH] E3DC-UPD-WEB-%s\n' "$exit_code" >&2
    printf 'Was ist passiert: %s\n' "$message" >&2
    printf 'Systemzustand: E3DC-Produktdateien und laufende E3DC-Dienste wurden nicht verändert.\n' >&2
    printf 'Lösung: %s\n' "$solution" >&2
    exit "$exit_code"
}

require_root_path() {
    local path="$1"
    local expected_mode="$2"
    local expected_group="$3"
    local owner group mode kind links
    IFS=' ' read -r owner group mode kind links < <(/usr/bin/stat -c '%u %g %a %F %h' -- "$path") \
        || fail "Pfadmetadaten fehlen: ${path}" 126
    [[ "$owner" == "0" && "$group" == "$expected_group" ]] \
        || fail "Pfad ist nicht root-eigen: ${path}" 126
    [[ "$mode" == "$expected_mode" ]] \
        || fail "Pfadmodus weicht ab: ${path}" 126
    [[ "$kind" == "directory" || ( "$kind" == "regular file" && "$links" == "1" ) ]] \
        || fail "Pfadtyp ist nicht zulässig: ${path}" 126
}

require_secure_root_directory() {
    local path="$1"
    local owner group mode kind
    [[ ! -L "$path" ]] || fail "System-Elternpfad ist ein Symlink: ${path}" 126
    IFS=' ' read -r owner group mode kind < <(/usr/bin/stat -c '%u %g %a %F' -- "$path") \
        || fail "System-Elternpfad ist nicht prüfbar: ${path}" 126
    [[ "$owner" == "0" && "$kind" == "directory" ]] \
        || fail "System-Elternpfad ist nicht root-kontrolliert: ${path}" 126
}

prepare_runtime_paths() {
    local www_data_gid
    www_data_gid="$(/usr/bin/getent group www-data | /usr/bin/cut -d: -f3)" \
        || fail "www-data-Gruppe konnte nicht gelesen werden" 1 \
            "Prüfe die Gruppe mit: getent group www-data ; starte danach erneut: sudo ${LAUNCHER}"
    [[ "$www_data_gid" =~ ^[0-9]+$ ]] || fail "www-data-Gruppe fehlt" 126
    [[ ! -L "$RUNTIME_DIR" && ! -L "$LOG_DIR" ]] \
        || fail "Runtime- oder Logpfad ist ein Symlink" 126
    require_secure_root_directory /run
    require_secure_root_directory /var/log
    /usr/bin/install -d -o root -g www-data -m 0750 -- "$RUNTIME_DIR" \
        || fail "Runtime-Verzeichnis konnte nicht angelegt oder repariert werden" 1 \
            "Führe sudo /usr/bin/install -d -o root -g www-data -m 0750 ${RUNTIME_DIR} aus und starte danach erneut: sudo ${LAUNCHER}"
    /usr/bin/install -d -o root -g root -m 0755 -- "$LOG_DIR" \
        || fail "Log-Verzeichnis konnte nicht angelegt oder repariert werden" 1 \
            "Führe sudo /usr/bin/install -d -o root -g root -m 0755 ${LOG_DIR} aus und starte danach erneut: sudo ${LAUNCHER}"
    require_root_path "$RUNTIME_DIR" 750 "$www_data_gid"
    require_root_path "$LOG_DIR" 755 0
}

prepare_lock_file() {
    local lock_path="$1"
    local metadata
    if [[ -e "$lock_path" || -L "$lock_path" ]]; then
        if [[ -L "$lock_path" ]]; then
            /usr/bin/unlink -- "$lock_path" \
                || fail "Unsicherer Update-Lock konnte nicht entfernt werden: ${lock_path}" 1 \
                    "Führe sudo /usr/bin/unlink ${lock_path} aus und starte danach erneut: sudo ${LAUNCHER}"
        else
            metadata="$(/usr/bin/stat -c '%u %g %F %h' -- "$lock_path")" \
                || fail "Update-Lock ist nicht prüfbar" 126
            if [[ "$metadata" != "0 0 regular file 1" \
                && "$metadata" != "0 0 regular empty file 1" ]]; then
                /usr/bin/unlink -- "$lock_path" \
                    || fail "Unsicherer Update-Lock konnte nicht ersetzt werden" 126
            fi
        fi
    fi
    if [[ ! -e "$lock_path" ]]; then
        (set -o noclobber; : > "$lock_path") 2>/dev/null || true
    fi
    metadata="$(/usr/bin/stat -c '%u %g %F %h' -- "$lock_path")" \
        || fail "Update-Lock konnte nicht atomar angelegt werden" 126
    [[ "$metadata" == "0 0 regular file 1" \
        || "$metadata" == "0 0 regular empty file 1" ]] \
        || fail "Update-Lock besitzt nach Anlage unzulässige Metadaten" 126
    /usr/bin/chown root:root -- "$lock_path" \
        || fail "Update-Lock konnte nicht root übergeben werden: ${lock_path}" 1 \
            "Führe sudo /usr/bin/chown root:root ${lock_path} aus und starte danach erneut: sudo ${LAUNCHER}"
    /usr/bin/chmod 0600 -- "$lock_path" \
        || fail "Update-Lock konnte nicht sicher gesetzt werden: ${lock_path}" 1 \
            "Führe sudo /usr/bin/chmod 0600 ${lock_path} aus und starte danach erneut: sudo ${LAUNCHER}"
}

write_runtime_value() {
    local target="$1"
    local value="$2"
    local temporary
    temporary="$(/usr/bin/mktemp "${RUNTIME_DIR}/.$(/usr/bin/basename "$target").XXXXXX")" \
        || return 1
    printf '%s\n' "$value" > "$temporary" \
        || return 1
    /usr/bin/chown root:www-data -- "$temporary" \
        || return 1
    /usr/bin/chmod 0640 -- "$temporary" \
        || return 1
    /usr/bin/mv -fT -- "$temporary" "$target" \
        || return 1
}

prepare_log() {
    local metadata
    if [[ -e "$LOG_FILE" || -L "$LOG_FILE" ]]; then
        [[ ! -L "$LOG_FILE" ]] || fail "Updateprotokoll ist ein Symlink" 126
        :
    else
        (set -o noclobber; : > "$LOG_FILE") 2>/dev/null || true
    fi
    metadata="$(/usr/bin/stat -c '%F %h' -- "$LOG_FILE")" \
        || fail "Updateprotokoll ist nicht prüfbar" 126
    [[ "$metadata" == "regular file 1" || "$metadata" == "regular empty file 1" ]] \
        || fail "Updateprotokoll besitzt einen unzulässigen Pfadtyp" 126
    /usr/bin/chown root:www-data -- "$LOG_FILE" \
        || fail "Updateprotokoll konnte nicht root:www-data übergeben werden" 1 \
            "Führe sudo /usr/bin/chown root:www-data ${LOG_FILE} aus und starte danach erneut: sudo ${LAUNCHER}"
    /usr/bin/chmod 0640 -- "$LOG_FILE" \
        || fail "Updateprotokoll konnte nicht sicher gesetzt werden" 1 \
            "Führe sudo /usr/bin/chmod 0640 ${LOG_FILE} aus und starte danach erneut: sudo ${LAUNCHER}"
    : > "$LOG_FILE" \
        || fail "Updateprotokoll konnte nicht geleert werden" 1 \
            "Prüfe den freien Speicher mit: df -h /var/log ; starte danach erneut: sudo ${LAUNCHER}"
    printf '=== E3DC-Control Update %s ===\n' "$(/usr/bin/date --iso-8601=seconds)" >> "$LOG_FILE"
}

validate_launcher_contract() {
    local metadata parent_mode launcher_owner launcher_mode launcher_kind launcher_links
    for binary in "$SYSTEMCTL" "$SYSTEMD_RUN" "$FLOCK" "$CURL" "$GIT" "$PYTHON" "$ENV"; do
        [[ -x "$binary" ]] || fail "Fest gebundenes Systemprogramm fehlt: ${binary}" 126
    done
    for parent in /usr/local /usr/local/sbin; do
        [[ ! -L "$parent" ]] \
            || fail "Launcher-Elternpfad ist ein Symlink: ${parent}" 126
        metadata="$(/usr/bin/stat -c '%u %g %a %F' -- "$parent")" \
            || fail "Launcher-Elternpfad ist nicht prüfbar: ${parent}" 126
        [[ "$metadata" =~ ^[0-9]+\ [0-9]+\ [0-7]+\ directory$ ]] \
            || fail "Launcher-Elternpfad ist kein echtes Verzeichnis: ${parent}" 126
        /usr/bin/chown root:root -- "$parent" \
            || fail "Launcher-Elternpfad konnte nicht root übergeben werden: ${parent}" 126
        /usr/bin/chmod 0755 -- "$parent" \
            || fail "Launcher-Elternpfad konnte nicht sicher gesetzt werden: ${parent}" 126
        metadata="$(/usr/bin/stat -c '%u %g %a %F' -- "$parent")" \
            || fail "Reparierter Launcher-Elternpfad ist nicht prüfbar: ${parent}" 126
        [[ "$metadata" == "0 0 755 directory" ]] \
            || fail "Launcher-Elternpfad blieb nach Reparatur unsicher: ${parent}" 126
        parent_mode="$(/usr/bin/stat -c '%a' -- "$parent")"
        [[ $((8#$parent_mode & 8#022)) -eq 0 ]] \
            || fail "Launcher-Elternpfad ist für Gruppe oder Andere schreibbar: ${parent}" 126
    done
    [[ ! -L "$LAUNCHER" ]] \
        || fail "Installierter Update-Dispatcher ist ein Symlink" 126
    launcher_owner="$(/usr/bin/stat -c '%u' -- "$LAUNCHER")" \
        || fail "Installierter Update-Dispatcher fehlt" 126
    launcher_mode="$(/usr/bin/stat -c '%a' -- "$LAUNCHER")"
    launcher_kind="$(/usr/bin/stat -c '%F' -- "$LAUNCHER")"
    launcher_links="$(/usr/bin/stat -c '%h' -- "$LAUNCHER")"
    [[ "$launcher_links" == "1" \
        && ( "$launcher_kind" == "regular file" || "$launcher_kind" == "regular empty file" ) ]] \
        || fail "Installierter Update-Dispatcher ist nicht regulär/nlink=1" 126
    /usr/bin/chown root:root -- "$LAUNCHER" \
        || fail "Installierter Update-Dispatcher konnte nicht root übergeben werden" 126
    /usr/bin/chmod 0755 -- "$LAUNCHER" \
        || fail "Installierter Update-Dispatcher konnte nicht sicher gesetzt werden" 126
    launcher_owner="$(/usr/bin/stat -c '%u' -- "$LAUNCHER")"
    launcher_mode="$(/usr/bin/stat -c '%a' -- "$LAUNCHER")"
    [[ "$launcher_owner" == "0" ]] \
        || fail "Installierter Update-Dispatcher blieb nach Reparatur fremdbesessen" 126
    [[ $((8#$launcher_mode & 8#022)) -eq 0 \
        && $((8#$launcher_mode & 8#500)) -eq $((8#500)) ]] \
        || fail "Installierter Update-Dispatcher ist nicht sicher root-lesbar/ausführbar" 126

}

isolated_git() {
    $ENV -i \
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
        "$GIT" \
        --no-pager \
        --no-replace-objects \
        -c credential.helper= \
        -c core.askPass=/bin/false \
        -c protocol.ext.allow=never \
        -c protocol.file.allow=never \
        "$@"
}

resolve_release_tag() {
    local effective_latest tag
    effective_latest="$($CURL -q \
        --fail --silent --show-error --location \
        --proto '=https' --tlsv1.2 \
        --output /dev/null --write-out '%{url_effective}' \
        "$LATEST_URL")" || fail "aktueller Release-Tag konnte nicht geladen werden"
    case "$effective_latest" in
        "$REPOSITORY_URL/releases/tag/"*) tag="${effective_latest##*/}" ;;
        *) fail "GitHub lieferte keinen eindeutigen aktuellen Release-Tag" ;;
    esac
    [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+[a-z]?$ ]] \
        || fail "Release-Tag besitzt ein unerwartetes Format: ${tag}"
    printf '%s\n' "$tag"
}

download_release_checkout() {
    local checkout="$1"
    local tag="$2"
    local fetched_tag fetched_sha head_sha metadata version required command_error
    local runtime_solution remote_solution retry_solution
    runtime_solution="Prüfe freien Speicher mit: df -h /run / ; starte danach erneut mit: sudo ${LAUNCHER}"
    remote_solution="Prüfe den GitHub-Zugriff mit: /usr/bin/curl -q -fsSI --proto '=https' --tlsv1.2 ${REPOSITORY_URL}/releases/tag/${tag} ; starte danach erneut mit: sudo ${LAUNCHER}"
    retry_solution="Starte den Release-Download erneut mit: sudo ${LAUNCHER} ; tritt derselbe Fehler erneut auf, zeige das Protokoll mit: sudo journalctl -u ${UNIT} --no-pager -n 200"
    [[ "$checkout" == /run/e3dc-update-download.*'/release' ]] \
        || fail "Release-Ziel liegt nicht im privaten Runtime-Pfad" 126
    [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+[a-z]?$ ]] \
        || fail "Release-Download besitzt keinen gültigen Release-Tag" 126
    if ! command_error="$(/usr/bin/mkdir -m 0700 -- "$checkout" 2>&1)"; then
        fail "Privates Release-Verzeichnis konnte nicht angelegt werden: ${command_error:-keine Detailausgabe}" 1 "$runtime_solution"
    fi
    if ! command_error="$(isolated_git -c init.defaultBranch=main init "$checkout" 2>&1)"; then
        fail "Privates Release-Verzeichnis konnte nicht als Git-Checkout initialisiert werden: ${command_error:-keine Detailausgabe}" 1 "$runtime_solution"
    fi
    if ! command_error="$(isolated_git -C "$checkout" remote add origin "$GIT_URL" 2>&1)"; then
        fail "Release-Quelle konnte im privaten Checkout nicht eingetragen werden: ${command_error:-keine Detailausgabe}" 1 "$runtime_solution"
    fi
    if ! command_error="$(isolated_git -C "$checkout" fetch \
        --no-tags --depth=1 origin \
        "+refs/tags/$tag:refs/tags/$tag" 2>&1)"; then
        fail "Release ${tag} konnte nicht geladen werden: ${command_error:-keine Detailausgabe}" 1 "$remote_solution"
    fi
    if ! fetched_tag="$(isolated_git -C "$checkout" rev-parse --verify "refs/tags/$tag" 2>&1)"; then
        fail "Geladener Release-Tag ${tag} konnte nicht aufgelöst werden: ${fetched_tag:-keine Detailausgabe}" 1 "$retry_solution"
    fi
    if ! fetched_sha="$(isolated_git -C "$checkout" rev-parse --verify "refs/tags/$tag^{commit}" 2>&1)"; then
        fail "Commit von Release ${tag} konnte nicht aufgelöst werden: ${fetched_sha:-keine Detailausgabe}" 1 "$retry_solution"
    fi
    [[ "$fetched_tag" =~ ^[0-9a-f]{40}$ && "$fetched_sha" =~ ^[0-9a-f]{40}$ ]] \
        || fail "Geladener Release-Tag besitzt keine eindeutige Commitbindung" 126
    isolated_git -C "$checkout" checkout --detach "$fetched_sha" >/dev/null \
        || fail "Gebundener Release-Commit konnte nicht ausgecheckt werden"
    if ! head_sha="$(isolated_git -C "$checkout" rev-parse --verify HEAD 2>&1)"; then
        fail "Ausgecheckter Release-Commit konnte nicht gelesen werden: ${head_sha:-keine Detailausgabe}" 1 "$retry_solution"
    fi
    [[ "$head_sha" == "$fetched_sha" ]] \
        || fail "Privater Release-Checkout widerspricht der Commitbindung" 126
    metadata="$(/usr/bin/stat -c '%u %g %a %F' -- "$checkout")" \
        || fail "Privater Release-Checkout ist nicht prüfbar" 126
    [[ "$metadata" == "0 0 700 directory" ]] \
        || fail "Privater Release-Checkout ist nicht ausschließlich root-zugänglich" 126
    for required in \
        VERSION e3dc-bootstrap e3dc-update-bootstrap \
        Installer/update_bootstrap_discovery.py Installer/update_simple.py
    do
        [[ -f "$checkout/$required" && ! -L "$checkout/$required" ]] \
            || fail "Release-Datei fehlt oder ist kein regulärer Pfad: ${required}" 126
    done
    version="$(<"$checkout/VERSION")"
    [[ -n "$version" && "v$version" == "$tag" ]] \
        || fail "VERSION und geladener Release-Tag widersprechen sich" 126
    printf '%s\t%s\n' "$fetched_tag" "$fetched_sha"
}

run_worker() {
    local result=1 release_binding tab tag tag_object target_sha release_dir
    [[ "${E3DC_WEB_UPDATE_WORKER:-}" == "1" && -n "${INVOCATION_ID:-}" ]] \
        || fail "Worker besitzt keinen systemd-Ausführungsvertrag" 126
    [[ -z "${SUDO_USER:-}" ]] || fail "Worker übernimmt keinen sudo-Aufrufer" 126

    prepare_runtime_paths
    prepare_lock_file "$LOCK_FILE"
    exec 9>"$LOCK_FILE"
    $FLOCK -n 9 || fail "Ein Update läuft bereits" 75
    cleanup() {
        local exit_code=$?
        set +e
        if [[ -n "${DOWNLOAD_DIR:-}" && -d "$DOWNLOAD_DIR" \
            && "$DOWNLOAD_DIR" == /run/e3dc-update-download.* ]]; then
            /usr/bin/find "$DOWNLOAD_DIR" -depth -delete
        fi
        write_runtime_value "$STATUS_FILE" "$exit_code" || true
        /usr/bin/unlink "$PID_FILE" 2>/dev/null || true
        return "$exit_code"
    }
    trap cleanup EXIT
    exec >> "$LOG_FILE" 2>&1
    write_runtime_value "$PID_FILE" "$$" \
        || fail "PID-Datei des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"
    write_runtime_value "$STATUS_FILE" "running" \
        || fail "Statusdatei des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"
    printf '%s\n' "[INFO] Update-Dispatcher-Vertrag: ${DISPATCHER_CONTRACT}"
    validate_launcher_contract

    tag="$(resolve_release_tag)"
    DOWNLOAD_DIR="$(/usr/bin/mktemp -d /run/e3dc-update-download.XXXXXX)" \
        || fail "Privates Download-Verzeichnis konnte nicht angelegt werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run / ; starte danach erneut: sudo ${LAUNCHER}"
    /usr/bin/chown root:root -- "$DOWNLOAD_DIR" \
        || fail "Privates Download-Verzeichnis konnte nicht root übergeben werden" 1 \
            "Starte denselben Updatebefehl erneut: sudo ${LAUNCHER}"
    /usr/bin/chmod 0700 -- "$DOWNLOAD_DIR" \
        || fail "Privates Download-Verzeichnis konnte nicht sicher gesetzt werden" 1 \
            "Starte denselben Updatebefehl erneut: sudo ${LAUNCHER}"
    release_dir="${DOWNLOAD_DIR}/release"
    release_binding="$(download_release_checkout "$release_dir" "$tag")"
    tab=$'\t'
    [[ "$release_binding" == *"$tab"* ]] \
        || fail "Release-Bindung ist unvollständig" 126
    tag_object="${release_binding%%"$tab"*}"
    target_sha="${release_binding#*"$tab"}"
    [[ "$target_sha" != *"$tab"* ]] \
        || fail "Release-Bindung besitzt überzählige Felder" 126
    printf '[OK] Release %s/%s mit einem privaten Fetch geladen.\n' \
        "$tag" "$target_sha"
    printf '[DETAIL] Lokale Git-Änderungen oder frühere Dateirechte blockieren den sicheren Reparaturweg nicht.\n'

    set +e
    $ENV -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/root \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        E3DC_BOOTSTRAP_ENTRY_MODE=regular \
        E3DC_BOOTSTRAP_EXPECTED_TAG="$tag" \
        E3DC_BOOTSTRAP_EXPECTED_TAG_OBJECT="$tag_object" \
        E3DC_BOOTSTRAP_EXPECTED_SHA="$target_sha" \
        E3DC_BOOTSTRAP_RELEASE_DIR="$release_dir" \
        E3DC_BOOTSTRAP_USER="$INSTALL_USER" \
        E3DC_BOOTSTRAP_INLINE_WORKER=1 \
        E3DC_BOOTSTRAP_LOCK_HELD=1 \
        /bin/sh "$release_dir/e3dc-update-bootstrap" "$INSTALL_ROOT"
    result=$?
    set -e
    if (( result != 0 )); then
        printf '[!] Update fehlgeschlagen (Exit %d).\n' "$result"
    fi
    return "$result"
}

update_job_running() {
    local active_state
    active_state="$($SYSTEMCTL show "$UNIT" --property=ActiveState --value 2>/dev/null || true)"
    case "$active_state" in
        active|activating|reloading) return 0 ;;
    esac
    $SYSTEMCTL --quiet is-active "$UNIT"
}

start_worker() {
    local launch_output launch_status
    (( $# == 0 )) || fail "Der Update-Dispatcher akzeptiert keine Argumente" 64
    validate_launcher_contract
    prepare_runtime_paths
    prepare_lock_file "$START_LOCK_FILE"
    exec 8>"$START_LOCK_FILE"
    $FLOCK 8
    if update_job_running; then
        printf 'Update läuft bereits: %s\n' "$UNIT"
        printf 'Status: systemctl status --no-pager %s\n' "$UNIT"
        printf 'Protokoll: journalctl -fu %s\n' "$UNIT"
        return 0
    fi
    prepare_log
    write_runtime_value "$STATUS_FILE" "launching" \
        || fail "Startstatus des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"
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
        --property=WorkingDirectory=/ \
        --property=UMask=0027 \
        --property=TimeoutStartSec=infinity \
        --setenv=E3DC_WEB_UPDATE_WORKER=1 \
        --setenv=SUDO_USER= \
        "$LAUNCHER" --worker 2>&1)"
    launch_status=$?
    set -e
    if (( launch_status != 0 )); then
        printf '[!] Update-Systemjob konnte nicht gestartet werden (Exit %d).\n%s\n' \
            "$launch_status" "$launch_output" >> "$LOG_FILE"
        write_runtime_value "$STATUS_FILE" "$launch_status" || true
        fail "Update-Systemjob konnte nicht gestartet werden" "$launch_status"
    fi
    [[ -z "$launch_output" ]] || printf '%s\n' "$launch_output"
    printf 'Update gestartet: %s\n' "$UNIT"
    printf 'Status: systemctl status --no-pager %s\n' "$UNIT"
    printf 'Protokoll: journalctl -fu %s\n' "$UNIT"
    printf 'Dateilog: tail -f %s\n' "$LOG_FILE"
}

if (( EUID != 0 )); then
    fail "Dispatcher benötigt Root-Rechte" 77
fi

if [[ "${1:-}" == "--worker" ]]; then
    (( $# == 1 )) || fail "Worker akzeptiert keine weiteren Argumente" 64
    run_worker
else
    start_worker "$@"
fi
