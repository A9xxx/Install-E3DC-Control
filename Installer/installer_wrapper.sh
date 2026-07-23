#!/bin/bash
# E3DC-Control Web Installer Wrapper
# Schmale Schleuse fuer den WebUI-Orchestrator.
# run_job bleibt read-only bzw. vom Python-Installer blockiert.
# run_write_job schaltet Schreibaktionen explizit fuer einen einzelnen
# Ramdisk-Job frei und darf nur ueber sudoers/Allowlist erreichbar sein.

set -u

ACTION=${1:-}
MODULE=${2:-}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="/usr/bin/python3"
PRODUCT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
PYTHON_BIN="$SYSTEM_PYTHON"
WEB_INSTALLER="${SCRIPT_DIR}/web_installer.py"
INSTALLER_MAIN="${SCRIPT_DIR}/../installer_main.py"

select_installer_python() {
    local selected
    if ! selected="$(
        E3DC_INSTALL_ROOT="$PRODUCT_ROOT" \
        PYTHONNOUSERSITE=1 \
        PYTHONPATH= \
        "$SYSTEM_PYTHON" -c \
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
    echo "Usage: $0 <catalog|diagnose|status|validate_config|update_check|run_job|run_write_job|check|fix_permissions|update_e3dc|install_release> [module/tag]"
}

if [ -z "$ACTION" ]; then
    usage
    exit 1
fi

case "$ACTION" in
    catalog|diagnose|status|validate_config|update_check|run_job|run_write_job|check|fix_permissions|update_e3dc|install_release)
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

if [[ "$ACTION" =~ ^(check|fix_permissions|update_e3dc|install_release)$ ]] && [ ! -f "$INSTALLER_MAIN" ]; then
    echo "installer_main.py nicht gefunden: $INSTALLER_MAIN"
    exit 1
fi

if [[ "$ACTION" =~ ^(check|fix_permissions|update_e3dc|install_release)$ ]]; then
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
        exec "$PYTHON_BIN" "$INSTALLER_MAIN" --check
        ;;
    fix_permissions)
        exec "$PYTHON_BIN" "$INSTALLER_MAIN" --fix-permissions
        ;;
    update_e3dc)
        exec "$PYTHON_BIN" "$INSTALLER_MAIN" --update-e3dc
        ;;
    install_release)
        if [ -z "$MODULE" ]; then
            echo "Release-Tag fehlt."
            exit 1
        fi
        exec "$PYTHON_BIN" "$INSTALLER_MAIN" --install-release-tag "$MODULE"
        ;;
esac
