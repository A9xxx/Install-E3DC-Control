#!/bin/bash
# E3DC-Control Installer Wrapper
# Administrativer Einstieg für bewusst gestartete Konsolenaktionen.
# Der frühere privilegierte WebUI-Einstieg bleibt gesperrt, bis ein eigener
# root-kontrollierter, aktionsgebundener Launcher verfügbar ist.

set -u

if [ "${SUDO_USER:-}" = "www-data" ]; then
    echo "Sicherheitssperre: Privilegierte Installer-Aufrufe aus dem Webserverkontext sind deaktiviert." >&2
    exit 126
fi

ACTION=${1:-}
MODULE=${2:-}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="/usr/bin/python3"
PRODUCT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PYTHON_BIN="$SYSTEM_PYTHON"
PYTHON_FLAGS=()
WEB_INSTALLER="${SCRIPT_DIR}/web_installer.py"
INSTALLER_MAIN="${SCRIPT_DIR}/../installer_main.py"

bind_release_system_python() {
    local resolved owner mode parent_owner parent_mode
    if ! resolved="$(readlink -f -- "$SYSTEM_PYTHON")"; then
        echo "Fester System-Python konnte nicht aufgelöst werden." >&2
        return 1
    fi
    if [[ ! "$resolved" =~ ^/usr/bin/python3([.][0-9]+)*$ ]] || [ ! -x "$resolved" ]; then
        echo "Fester System-Python besitzt kein freigegebenes Ziel: $resolved" >&2
        return 1
    fi
    if ! read -r owner mode < <(stat -Lc '%u %a' -- "$resolved"); then
        echo "Metadaten des System-Python sind nicht lesbar." >&2
        return 1
    fi
    if ! read -r parent_owner parent_mode < <(stat -Lc '%u %a' -- /usr/bin); then
        echo "Metadaten des System-Python-Pfads sind nicht lesbar." >&2
        return 1
    fi
    if [ "$owner" != "0" ] || [ "$parent_owner" != "0" ] \
        || (( (8#$mode & 0022) != 0 )) \
        || (( (8#$parent_mode & 0022) != 0 )); then
        echo "System-Python oder /usr/bin ist nicht ausschließlich root-kontrolliert." >&2
        return 1
    fi
    PYTHON_BIN="$resolved"
    PYTHON_FLAGS=(-I -B -u)
}

select_installer_python() {
    local selected
    if ! selected="$(
        E3DC_INSTALL_ROOT="$PRODUCT_ROOT" \
        PYTHONNOUSERSITE=1 \
        PYTHONPATH= \
        "$SYSTEM_PYTHON" -I -B -u -c \
        'import os,sys; root=os.path.realpath(sys.argv[1]); sys.path.insert(0,root); from Installer.update import select_wrapper_python; print(select_wrapper_python(sys.argv[2]))' \
        "$PRODUCT_ROOT" "$ACTION"
    )"; then
        echo "Vertrauenswürdiger Python-Interpreter für ${ACTION} konnte nicht gebunden werden." >&2
        return 1
    fi
    if [ -z "$selected" ] || [ "${selected#/}" = "$selected" ] || [ ! -x "$selected" ]; then
        echo "Ungültiger Python-Interpreter für ${ACTION}." >&2
        return 1
    fi
    PYTHON_BIN="$selected"
}

usage() {
    echo "Usage: $0 <catalog|diagnose|status|validate_config|update_check|run_job|run_write_job|check|fix_permissions|update_e3dc|reinstall_current|install_release> [module/tag]"
}

if [ -z "$ACTION" ]; then
    usage
    exit 1
fi

case "$ACTION" in
    catalog|diagnose|status|validate_config|update_check|run_job|run_write_job|check|fix_permissions|update_e3dc|reinstall_current|install_release)
        ;;
    *)
        echo "Unzulaessige Installer-Aktion: $ACTION"
        exit 1
        ;;
esac

if [ -n "$MODULE" ] && [[ ! "$MODULE" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "Unzulaessiges Modul: $MODULE"
    exit 1
fi

if [ ! -f "$WEB_INSTALLER" ]; then
    echo "web_installer.py nicht gefunden: $WEB_INSTALLER"
    exit 1
fi

if [[ "$ACTION" =~ ^(check|fix_permissions|update_e3dc|reinstall_current|install_release)$ ]] && [ ! -f "$INSTALLER_MAIN" ]; then
    echo "installer_main.py nicht gefunden: $INSTALLER_MAIN"
    exit 1
fi

if [[ "$ACTION" =~ ^(update_e3dc|reinstall_current|install_release)$ ]]; then
    bind_release_system_python || exit 1
elif [[ "$ACTION" =~ ^(check|fix_permissions)$ ]]; then
    select_installer_python || exit 1
fi

case "$ACTION" in
    catalog)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --action catalog
        ;;
    diagnose)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --action run_diagnosis ${MODULE:+--module "$MODULE"}
        ;;
    status)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --action status ${MODULE:+--module "$MODULE"}
        ;;
    validate_config)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --action validate_config ${MODULE:+--module "$MODULE"}
        ;;
    update_check)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --action update_check
        ;;
    run_job)
        exec "$PYTHON_BIN" "$WEB_INSTALLER" --job-file
        ;;
    run_write_job)
        E3DC_WEB_INSTALLER_ENABLE_WRITES=1 exec "$PYTHON_BIN" "$WEB_INSTALLER" --job-file
        ;;
    check)
        exec "$PYTHON_BIN" "${PYTHON_FLAGS[@]}" "$INSTALLER_MAIN" --check
        ;;
    fix_permissions)
        exec "$PYTHON_BIN" "${PYTHON_FLAGS[@]}" "$INSTALLER_MAIN" --fix-permissions
        ;;
    update_e3dc)
        exec "$PYTHON_BIN" "${PYTHON_FLAGS[@]}" "$INSTALLER_MAIN" --update-e3dc
        ;;
    reinstall_current)
        exec "$PYTHON_BIN" "${PYTHON_FLAGS[@]}" "$INSTALLER_MAIN" --reinstall-current
        ;;
    install_release)
        if [ -z "$MODULE" ]; then
            echo "Release-Tag fehlt."
            exit 1
        fi
        exec "$PYTHON_BIN" "${PYTHON_FLAGS[@]}" "$INSTALLER_MAIN" --install-release-tag "$MODULE"
        ;;
esac
