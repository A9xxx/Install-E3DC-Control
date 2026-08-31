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
export LANG=C
export LC_ALL=C

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
readonly START_ACK_FILE="${RUNTIME_DIR}/start.ack"
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
readonly SLEEP="/usr/bin/sleep"
readonly WORKER_UNIT_IDENTITY_ATTEMPTS=50
readonly START_ACK_ATTEMPTS=150
readonly EXECUTION_ACK_ATTEMPTS=50
readonly WORKER_ACK_ATTEMPTS=200
readonly ACK_INTERVAL_SECONDS="0.1"
DOWNLOAD_DIR=""
UNIT_ACTIVE_STATE=""
UNIT_SUB_STATE=""
UNIT_RESULT=""
UNIT_EXEC_MAIN_STATUS=""
UNIT_MAIN_PID=""
UNIT_LOAD_STATE=""
CONFIRMED_WORKER_PID=""
EXECUTION_PATH_MAY_HAVE_STARTED=0

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
    if (( EXECUTION_PATH_MAY_HAVE_STARTED == 1 )); then
        printf '%s\n' \
            'Systemzustand: Der Worker wurde zur Ausführung freigegeben. Der erreichte Anlagen- und Produktzustand muss anhand Updateprotokoll, Backup/Rollback und Abschlussprüfung bestimmt werden; ein erfolgreicher Abschluss wurde nicht bestätigt.' \
            >&2
    else
        printf 'Systemzustand: E3DC-Produktdateien und laufende E3DC-Dienste wurden nicht verändert.\n' >&2
    fi
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

read_runtime_value() {
    local source="$1"
    local value=""
    [[ -f "$source" && ! -L "$source" ]] || return 1
    IFS= read -r value < "$source" || [[ -n "$value" ]] || return 1
    printf '%s\n' "$value"
}

load_update_unit_state() {
    local snapshot key value
    UNIT_ACTIVE_STATE=""
    UNIT_SUB_STATE=""
    UNIT_RESULT=""
    UNIT_EXEC_MAIN_STATUS=""
    UNIT_MAIN_PID=""
    UNIT_LOAD_STATE=""
    snapshot="$($SYSTEMCTL show "$UNIT" \
        --property=LoadState \
        --property=ActiveState \
        --property=SubState \
        --property=Result \
        --property=ExecMainStatus \
        --property=MainPID 2>/dev/null || true)"
    while IFS='=' read -r key value; do
        case "$key" in
            LoadState) UNIT_LOAD_STATE="$value" ;;
            ActiveState) UNIT_ACTIVE_STATE="$value" ;;
            SubState) UNIT_SUB_STATE="$value" ;;
            Result) UNIT_RESULT="$value" ;;
            ExecMainStatus) UNIT_EXEC_MAIN_STATUS="$value" ;;
            MainPID) UNIT_MAIN_PID="$value" ;;
        esac
    done <<< "$snapshot"
}

normalize_start_failure_exit() {
    local value="$1"
    if [[ "$value" =~ ^[1-9][0-9]*$ ]] \
        && (( 10#$value >= 1 && 10#$value <= 255 )); then
        printf '%d\n' "$((10#$value))"
    else
        printf '%d\n' 1
    fi
}

record_start_failure() {
    local exit_code reason detail
    exit_code="$(normalize_start_failure_exit "$1")"
    reason="$2"
    detail="$3"
    [[ "$reason" =~ ^[a-z0-9_]+$ ]] || reason="unknown_start_failure"
    printf '[FEHLER] Update-Worker nicht gestartet: %s (Exit %d, Grund %s).\n' \
        "$detail" "$exit_code" "$reason" >> "$LOG_FILE"
    printf '%s\n' \
        '[FEHLER] Anlage/Produktdateien unverändert: Der Worker erhielt vor Releaseauflösung, Download und Bootstrap keine gültige Startfreigabe.' \
        >> "$LOG_FILE"
    write_runtime_value "$STATUS_FILE" "start_failed:${exit_code}:${reason}" \
        || true
}

worker_phase_identity_confirmed() {
    local expected_status="$1"
    local raw_status worker_pid
    CONFIRMED_WORKER_PID=""
    raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
    worker_pid="$(read_runtime_value "$PID_FILE" 2>/dev/null || true)"
    load_update_unit_state
    [[ "$raw_status" == "$expected_status" ]] || return 1
    [[ "$worker_pid" =~ ^[1-9][0-9]*$ && -d "/proc/${worker_pid}" ]] \
        || return 1
    [[ "$UNIT_MAIN_PID" == "$worker_pid" ]] || return 1
    case "$UNIT_ACTIVE_STATE" in
        active|activating|reloading) ;;
        *) return 1 ;;
    esac
    CONFIRMED_WORKER_PID="$worker_pid"
    return 0
}

worker_start_identity_confirmed() {
    worker_phase_identity_confirmed "running"
}

worker_execution_identity_confirmed() {
    worker_phase_identity_confirmed "executing"
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
    for binary in \
        "$SYSTEMCTL" "$SYSTEMD_RUN" "$FLOCK" "$CURL" "$GIT" \
        "$PYTHON" "$ENV" "$SLEEP"
    do
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
    local attempt ack_value current_status unit_identity_confirmed=0 worker_acknowledged=0
    local worker_cleanup_started=0
    worker_signal_exit() {
        local signal_name="$1"
        local signal_exit="$2"
        trap '' HUP INT TERM
        if (( worker_acknowledged == 1 )); then
            printf '[FEHLER] Update-Worker erhielt Signal %s nach bestätigtem Start (Exit %d). Der erreichte Updatezustand muss anhand Dateilog, Backup-/Rollback- und Abschlussprüfung bestimmt werden.\n' \
                "$signal_name" "$signal_exit" >&2
        else
            printf '[FEHLER] Update-Worker erhielt Signal %s vor der Startfreigabe (Exit %d). Anlage/Produktdateien unverändert; Releaseauflösung, Download und Bootstrap wurden nicht freigegeben.\n' \
                "$signal_name" "$signal_exit" >&2
        fi
        exit "$signal_exit"
    }
    cleanup() {
        local exit_code=$? status_before=""
        trap - EXIT
        trap '' HUP INT TERM
        if (( worker_cleanup_started == 1 )); then
            exit "$exit_code"
        fi
        worker_cleanup_started=1
        set +e
        status_before="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        /usr/bin/unlink "$START_ACK_FILE" 2>/dev/null || true
        /usr/bin/unlink "$PID_FILE" 2>/dev/null || true
        if [[ -n "${DOWNLOAD_DIR:-}" && -d "$DOWNLOAD_DIR" \
            && "$DOWNLOAD_DIR" == /run/e3dc-update-download.* ]]; then
            /usr/bin/find "$DOWNLOAD_DIR" -depth -delete
        fi
        if (( worker_acknowledged == 1 )) \
            || [[ "$status_before" != start_failed:* ]]; then
            write_runtime_value "$STATUS_FILE" "$exit_code" || true
        fi
        exit "$exit_code"
    }
    trap cleanup EXIT
    trap 'worker_signal_exit HUP 129' HUP
    trap 'worker_signal_exit INT 130' INT
    trap 'worker_signal_exit TERM 143' TERM

    prepare_runtime_paths
    [[ -f "$LOG_FILE" && ! -L "$LOG_FILE" ]] \
        || fail "Sicheres Updateprotokoll fehlt vor dem Worker-Start" 126
    exec >> "$LOG_FILE" 2>&1
    prepare_lock_file "$LOCK_FILE"
    exec 9>"$LOCK_FILE"
    $FLOCK -n 9 || fail "Ein Update läuft bereits" 75
    [[ "${E3DC_WEB_UPDATE_WORKER:-}" == "1" ]] \
        || fail "Worker besitzt keinen expliziten Dispatcher-Auftrag" 126
    [[ -z "${SUDO_USER:-}" ]] || fail "Worker übernimmt keinen sudo-Aufrufer" 126

    # INVOCATION_ID bleibt nur ein optionaler systemd-Hinweis. Die tatsächliche
    # Bindung ist stärker: Diese Shell muss der MainPID genau dieser aktiven
    # transienten Unit entsprechen. Noch vor PID-/Running-Marker und
    # Produktzugriff wird das begrenzt geprüft.
    for ((attempt = 0; attempt < WORKER_UNIT_IDENTITY_ATTEMPTS; attempt++)); do
        load_update_unit_state
        if [[ "$UNIT_MAIN_PID" == "$$" ]]; then
            case "$UNIT_ACTIVE_STATE" in
                active|activating|reloading)
                    unit_identity_confirmed=1
                    break
                    ;;
            esac
        fi
        if [[ -n "$UNIT_RESULT" && "$UNIT_RESULT" != "success" ]]; then
            break
        fi
        $SLEEP "$ACK_INTERVAL_SECONDS"
    done
    (( unit_identity_confirmed == 1 )) \
        || fail "Worker konnte nicht eindeutig an ${UNIT} gebunden werden" 126

    write_runtime_value "$PID_FILE" "$$" \
        || fail "PID-Datei des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"
    write_runtime_value "$STATUS_FILE" "running" \
        || fail "Statusdatei des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"

    # Der Worker darf Releaseauflösung, Download und Bootstrap erst nach dem
    # Rück-Ack des Elternprozesses beginnen. So bleibt ein nicht bestätigter
    # Start nachweislich ohne Produkt- oder Dienstmutation.
    for ((attempt = 0; attempt < WORKER_ACK_ATTEMPTS; attempt++)); do
        ack_value="$(read_runtime_value "$START_ACK_FILE" 2>/dev/null || true)"
        if [[ "$ack_value" == "$$" ]]; then
            worker_acknowledged=1
            EXECUTION_PATH_MAY_HAVE_STARTED=1
            write_runtime_value "$STATUS_FILE" "executing" \
                || fail "Ausführungsphase des Updatejobs konnte nicht veröffentlicht werden" 1 \
                    "Prüfe den freien Speicher mit: df -h /run ; prüfe danach Protokoll und Updatezustand vor einem erneuten Start."
            /usr/bin/unlink "$START_ACK_FILE" 2>/dev/null || true
            break
        fi
        current_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        if [[ "$current_status" == start_failed:* ]]; then
            fail "Dispatcher hat die Worker-Startphase abgebrochen (${current_status})" 70
        fi
        $SLEEP "$ACK_INTERVAL_SECONDS"
    done
    if (( worker_acknowledged != 1 )); then
        record_start_failure 70 "launcher_ack_timeout" \
            "Rückbestätigung des gebundenen Workers blieb 20 Sekunden aus"
        fail "Rückbestätigung des gebundenen Workers blieb aus" 70
    fi

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
    load_update_unit_state
    # Eine geladene Transient-Unit gehört noch dem vorherigen Auftrag, auch
    # wenn sie bereits inactive/failed ist und --collect sie gerade entfernt.
    # Log und Runtime-Evidenz dürfen in diesem kurzen Fenster nicht
    # überschrieben werden.
    [[ "$UNIT_LOAD_STATE" == "loaded" ]] && return 0
    case "$UNIT_ACTIVE_STATE" in
        active|activating|reloading|deactivating) return 0 ;;
    esac
    $SYSTEMCTL --quiet is-active "$UNIT"
}

start_worker() {
    local launch_output launch_status launch_failure_exit=1
    local launch_failure_reason="" launch_failure_detail=""
    local raw_status worker_pid attempt start_acknowledged=0 execution_acknowledged=0
    (( $# == 0 )) || fail "Der Update-Dispatcher akzeptiert keine Argumente" 64
    validate_launcher_contract
    prepare_runtime_paths
    prepare_lock_file "$START_LOCK_FILE"
    exec 8>"$START_LOCK_FILE"
    $FLOCK 8
    if update_job_running; then
        # Ein bereits aktiver Job kann aus einem älteren Launcher stammen oder
        # sich zwischen Statusphasen befinden. Ab hier ist der Gesamtzustand
        # deshalb konservativ nicht mehr als unverändert beweisbar.
        EXECUTION_PATH_MAY_HAVE_STARTED=1
        if worker_execution_identity_confirmed; then
            printf 'Update läuft bereits in bestätigter Ausführungsphase: %s\n' "$UNIT"
            printf 'Status: systemctl status --no-pager %s\n' "$UNIT"
            printf 'Protokoll: journalctl -fu %s\n' "$UNIT"
            return 0
        fi
        worker_start_identity_confirmed \
            || fail "Aktive Update-Unit ist weder als Start- noch als Ausführungsphase eindeutig durch Status, PID und systemd-MainPID bestätigt" 75 \
                "Prüfe zuerst: systemctl status --no-pager ${UNIT} ; starte erst nach geklärtem Zustand erneut."
        write_runtime_value "$START_ACK_FILE" "$CONFIRMED_WORKER_PID" \
            || fail "Startbestätigung des bereits laufenden Updatejobs konnte nicht geschrieben werden" 1
        EXECUTION_PATH_MAY_HAVE_STARTED=1
        for ((attempt = 0; attempt < EXECUTION_ACK_ATTEMPTS; attempt++)); do
            if worker_execution_identity_confirmed; then
                printf 'Update läuft bereits und hat die Ausführungsphase bestätigt: %s\n' "$UNIT"
                printf 'Status: systemctl status --no-pager %s\n' "$UNIT"
                printf 'Protokoll: journalctl -fu %s\n' "$UNIT"
                return 0
            fi
            raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
            if [[ "$raw_status" =~ ^-?[0-9]+$ ]]; then
                if [[ "$raw_status" == "0" ]]; then
                    printf 'Update wurde nach bestätigter Startfreigabe bereits abgeschlossen: %s\n' "$UNIT"
                    return 0
                fi
                fail "Bereits laufender Update-Worker endete nach Startfreigabe mit Exit ${raw_status}; erreichten Zustand prüfen und nicht blind erneut starten" \
                    "$(normalize_start_failure_exit "$raw_status")"
            fi
            $SLEEP "$ACK_INTERVAL_SECONDS"
        done
        fail "Bereits laufender Worker bestätigte die Ausführungsphase nach Startfreigabe nicht; Zustand prüfen und nicht blind erneut starten" 75
    fi
    prepare_log
    write_runtime_value "$STATUS_FILE" "launching" \
        || fail "Startstatus des Updatejobs konnte nicht geschrieben werden" 1 \
            "Prüfe den freien Speicher mit: df -h /run ; starte danach erneut: sudo ${LAUNCHER}"
    /usr/bin/unlink "$PID_FILE" 2>/dev/null || true
    /usr/bin/unlink "$START_ACK_FILE" 2>/dev/null || true
    $SYSTEMCTL reset-failed "$UNIT" 2>/dev/null || true
    set +e
    launch_output="$($SYSTEMD_RUN \
        --unit="${UNIT%.service}" \
        --collect \
        --property=Type=exec \
        --property=User=root \
        --property=Group=root \
        --property=WorkingDirectory=/ \
        --property=UMask=0027 \
        --property=TimeoutStartSec=20s \
        --property="StandardOutput=append:${LOG_FILE}" \
        --property="StandardError=append:${LOG_FILE}" \
        --setenv=E3DC_WEB_UPDATE_WORKER=1 \
        --setenv=SUDO_USER= \
        "$LAUNCHER" --worker 2>&1)"
    launch_status=$?
    set -e
    if (( launch_status != 0 )); then
        printf '[!] Update-Systemjob konnte nicht gestartet werden (Exit %d).\n%s\n' \
            "$launch_status" "$launch_output" >> "$LOG_FILE"
        launch_failure_exit="$(normalize_start_failure_exit "$launch_status")"
        record_start_failure "$launch_failure_exit" "systemd_run_failed" \
            "systemd-run bestätigte den Type=exec-Start nicht"
        $SYSTEMCTL stop "$UNIT" >> "$LOG_FILE" 2>&1 || true
        write_runtime_value "$STATUS_FILE" \
            "start_failed:${launch_failure_exit}:systemd_run_failed" || true
        fail "Update-Systemjob konnte nicht gestartet werden (systemd_run_failed); Anlage/Produktdateien unverändert" \
            "$launch_failure_exit"
    fi

    for ((attempt = 0; attempt < START_ACK_ATTEMPTS; attempt++)); do
        if worker_start_identity_confirmed; then
            if write_runtime_value "$START_ACK_FILE" "$CONFIRMED_WORKER_PID"; then
                start_acknowledged=1
                EXECUTION_PATH_MAY_HAVE_STARTED=1
                break
            fi
            launch_failure_reason="worker_ack_write_failed"
            launch_failure_detail="Bestätigte Worker-PID konnte nicht atomar rückbestätigt werden"
            break
        fi

        raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        worker_pid="$(read_runtime_value "$PID_FILE" 2>/dev/null || true)"
        if [[ "$raw_status" == start_failed:* ]]; then
            IFS=':' read -r _ launch_failure_exit launch_failure_reason <<< "$raw_status"
            launch_failure_exit="$(normalize_start_failure_exit "$launch_failure_exit")"
            [[ "$launch_failure_reason" =~ ^[a-z0-9_]+$ ]] \
                || launch_failure_reason="worker_start_failed"
            launch_failure_detail="Worker meldete bereits ${raw_status}"
            break
        fi
        if [[ "$raw_status" =~ ^-?[0-9]+$ ]]; then
            launch_failure_exit="$(normalize_start_failure_exit "$raw_status")"
            launch_failure_reason="worker_exit_before_ack"
            launch_failure_detail="Worker endete vor der gebundenen Startbestätigung mit Exit ${raw_status}"
            break
        fi
        if [[ "$UNIT_ACTIVE_STATE" == "failed" \
            || ( -n "$UNIT_RESULT" && "$UNIT_RESULT" != "success" ) ]]; then
            launch_failure_exit="$(normalize_start_failure_exit "$UNIT_EXEC_MAIN_STATUS")"
            launch_failure_reason="worker_exec_failed"
            launch_failure_detail="systemd meldete ActiveState=${UNIT_ACTIVE_STATE:-unbekannt}, Result=${UNIT_RESULT:-unbekannt}, ExecMainStatus=${UNIT_EXEC_MAIN_STATUS:-unbekannt}"
            break
        fi
        $SLEEP "$ACK_INTERVAL_SECONDS"
    done

    if (( start_acknowledged != 1 )); then
        raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        worker_pid="$(read_runtime_value "$PID_FILE" 2>/dev/null || true)"
        if [[ -z "$launch_failure_reason" ]]; then
            launch_failure_exit=1
            if [[ "$raw_status" == "running" ]]; then
                launch_failure_reason="worker_pid_invalid"
                launch_failure_detail="Status war running, aber PID ${worker_pid:-fehlt} passte nicht zur aktiven systemd-MainPID ${UNIT_MAIN_PID:-fehlt}"
            else
                launch_failure_reason="worker_start_timeout"
                launch_failure_detail="Nach 15 Sekunden fehlten frische PID, running-Status und passende systemd-MainPID"
            fi
        fi
        record_start_failure "$launch_failure_exit" "$launch_failure_reason" \
            "$launch_failure_detail"
        $SYSTEMCTL stop "$UNIT" >> "$LOG_FILE" 2>&1 || true
        write_runtime_value "$STATUS_FILE" \
            "start_failed:$(normalize_start_failure_exit "$launch_failure_exit"):${launch_failure_reason}" \
            || true
        fail "Update-Worker wurde nicht bestätigt (${launch_failure_reason}); Anlage/Produktdateien unverändert" \
            "$(normalize_start_failure_exit "$launch_failure_exit")"
    fi

    # Der Rück-ACK allein öffnet den Produktpfad noch nicht. Erst der vom
    # gebundenen Worker atomar veröffentlichte executing-Status beweist, dass
    # er die Freigabe übernommen hat. Ab hier darf kein Fehler mehr pauschal
    # behaupten, Anlage oder Produktdateien seien unverändert.
    for ((attempt = 0; attempt < EXECUTION_ACK_ATTEMPTS; attempt++)); do
        if worker_execution_identity_confirmed; then
            execution_acknowledged=1
            break
        fi
        raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        if [[ "$raw_status" =~ ^-?[0-9]+$ ]]; then
            if [[ "$raw_status" == "0" ]]; then
                printf '%s\n' "$launch_output"
                printf 'Update wurde nach bestätigter Startfreigabe bereits abgeschlossen: %s\n' "$UNIT"
                return 0
            fi
            fail "Update-Worker endete nach bestätigter Startfreigabe mit Exit ${raw_status}; erreichten Anlagen-/Produktzustand anhand Protokoll, Backup/Rollback und Abschlussprüfung bestimmen und nicht blind erneut starten" \
                "$(normalize_start_failure_exit "$raw_status")"
        fi
        if [[ "$raw_status" == start_failed:* ]]; then
            fail "Update-Worker meldete nach bestätigter Startfreigabe ${raw_status}; erreichten Anlagen-/Produktzustand prüfen und nicht blind erneut starten" 75
        fi
        $SLEEP "$ACK_INTERVAL_SECONDS"
    done
    if (( execution_acknowledged != 1 )); then
        raw_status="$(read_runtime_value "$STATUS_FILE" 2>/dev/null || true)"
        fail "Ausführungsphase wurde nach bestätigter Startfreigabe nicht eindeutig gebunden (Status ${raw_status:-fehlt}); Zustand anhand Protokoll und systemd prüfen und nicht blind erneut starten" 75
    fi
    [[ -z "$launch_output" ]] || printf '%s\n' "$launch_output"
    printf 'Update gestartet und durch PID/status/systemd sowie Ausführungsphase bestätigt: %s\n' "$UNIT"
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
