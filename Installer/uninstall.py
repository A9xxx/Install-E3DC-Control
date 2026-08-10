import os
import pwd
import shlex
import stat
import subprocess
import shutil

from .apache_security import (
    CONF_DESTINATION,
    CONF_ENABLED,
    CONF_NAME,
    remove_apache_live_access_log_filter,
)
from .core import register_command
from .utils import (
    MANAGER_LOCK_TMPFILES_CONFIG,
    MANAGER_LOCK_TMPFILES_CONTENT,
    resolve_venv_target,
    run_command,
)
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
uninstall_logger = get_or_create_logger("uninstall")
APACHE_SECURITY_REMOVED = "removed"
APACHE_SECURITY_ABSENT = "absent"
APACHE_SECURITY_RETAINED = "retained"
APACHE_SECURITY_FAILED = "failed"
APACHE_PROTECTED_WEBROOT_PATHS = (
    "/var/www/html/data",
    "/var/www/html/logs",
    "/var/www/html/ramdisk",
    "/var/www/html/tmp",
    "/var/www/html/history_backups",
    "/var/www/html/live_history.txt",
    "/var/www/html/e3dc_paths.json",
    "/var/www/html/e3dc.config.txt",
    "/var/www/html/e3dc.strompreise.txt",
    "/var/www/html/e3dc.wallbox.txt",
    "/var/www/html/e3dc.wallbox.out",
)
HARDWARE_WRITER_SERVICE_NAMES = frozenset({
    "e3dc",
    "energy_manager",
    "e3dc-ha",
    "e3dc-shadow-sync",
    "e3dc-storage-manager",
    "e3dc-wallbox-manager",
    "e3dc-heizstab",
    "e3dc-climate-control",
})


def _service_unit_exists(service_name):
    unit = str(service_name).removesuffix(".service") + ".service"
    return any(
        os.path.lexists(os.path.join(base, unit))
        for base in (
            "/etc/systemd/system",
            "/run/systemd/system",
            "/lib/systemd/system",
            "/usr/lib/systemd/system",
        )
    )


def _service_is_proven_inactive(service_name):
    probe = run_command(
        "systemctl is-active " + shlex.quote(str(service_name)),
        timeout=10,
    )
    activity = str((probe or {}).get("stdout") or "").strip().lower()
    if activity in {"inactive", "failed"}:
        return True
    return activity == "unknown" and not _service_unit_exists(service_name)


def _service_is_proven_not_enabled(service_name):
    probe = run_command(
        "systemctl is-enabled " + shlex.quote(str(service_name)),
        timeout=10,
    )
    state = str((probe or {}).get("stdout") or "").strip().lower()
    if state in {"disabled", "masked", "static", "indirect", "generated", "transient"}:
        return True
    return state in {"not-found", "unknown", ""} and not _service_unit_exists(service_name)


def _remove_managed_docker_container():
    """Stoppt den einzigen Produktcontainer und beweist danach seine Abwesenheit."""

    inspect = run_command(
        "sudo docker inspect -f '{{.State.Running}}' e3dc-control",
        timeout=30,
    )
    if not isinstance(inspect, dict):
        return False
    if not inspect.get("success"):
        listed = run_command(
            "sudo docker ps -a --filter 'name=^/e3dc-control$' --format '{{.Names}}'",
            timeout=30,
        )
        return bool(
            isinstance(listed, dict)
            and listed.get("success")
            and str(listed.get("stdout") or "").strip() == ""
        )

    running = str(inspect.get("stdout") or "").strip().lower() == "true"
    if running:
        stopped = run_command("sudo docker stop e3dc-control", timeout=60)
        if not isinstance(stopped, dict) or not stopped.get("success"):
            return False
        stopped_probe = run_command(
            "sudo docker inspect -f '{{.State.Running}}' e3dc-control",
            timeout=30,
        )
        if (
            not isinstance(stopped_probe, dict)
            or not stopped_probe.get("success")
            or str(stopped_probe.get("stdout") or "").strip().lower() != "false"
        ):
            return False

    removed = run_command("sudo docker rm e3dc-control", timeout=60)
    if not isinstance(removed, dict) or not removed.get("success"):
        return False
    final_probe = run_command(
        "sudo docker ps -a --filter 'name=^/e3dc-control$' --format '{{.Names}}'",
        timeout=30,
    )
    return bool(
        isinstance(final_probe, dict)
        and final_probe.get("success")
        and str(final_probe.get("stdout") or "").strip() == ""
    )


def remove_cron_pattern(pattern):
    """Entfernt Zeilen aus der Crontab, die das Pattern enthalten."""
    try:
        install_user = get_install_user()
        result = run_command(f"sudo -u {install_user} crontab -l", timeout=5)

        if result['success']:
            lines = result['stdout'].splitlines()
            # Behalte Zeilen, die das Pattern NICHT enthalten
            new_lines = [l for l in lines if pattern not in l and l.strip()]

            # Wenn sich die Anzahl geändert hat, schreiben wir neu
            if len(lines) != len(new_lines):
                new_cron = "\n".join(new_lines) + "\n"
                if not new_lines:
                     # Wenn leer, crontab entfernen
                     run_command(f"sudo -u {install_user} crontab -r", timeout=5)
                else:
                    process = subprocess.Popen(
                        ["sudo", "-u", install_user, "crontab", "-"],
                        stdin=subprocess.PIPE,
                        text=True
                    )
                    process.communicate(input=new_cron, timeout=10)
                return True
    except Exception as e:
        log_warning("uninstall", f"Fehler beim Entfernen von Cron-Pattern '{pattern}': {e}")
    return False


def _remaining_apache_protected_webroot_paths():
    return tuple(
        path
        for path in APACHE_PROTECTED_WEBROOT_PATHS
        if os.path.lexists(path)
    )


def _apache_security_entries_safe_for_removal():
    """Akzeptiert ausschließlich den vom Produkt verwalteten Root-Vertrag."""

    destination = str(CONF_DESTINATION)
    enabled = str(CONF_ENABLED)
    if os.path.lexists(enabled):
        try:
            metadata = os.lstat(enabled)
            raw_target = os.readlink(enabled)
            resolved_target = (
                raw_target
                if os.path.isabs(raw_target)
                else os.path.join(os.path.dirname(enabled), raw_target)
            )
        except OSError:
            return False
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or os.path.abspath(resolved_target) != os.path.abspath(destination)
        ):
            return False

    if os.path.lexists(destination):
        try:
            metadata = os.lstat(destination)
        except OSError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_nlink != 1
            or metadata.st_size > 64 * 1024
        ):
            return False
    return True


def remove_apache_runtime_path_protection():
    """Deaktiviert und entfernt die Apache-Sperre als geprüfte Transaktion."""

    destination = str(CONF_DESTINATION)
    enabled = str(CONF_ENABLED)
    if not os.path.lexists(destination) and not os.path.lexists(enabled):
        if remove_apache_live_access_log_filter(
            run_command,
            reload_apache=True,
        ):
            return APACHE_SECURITY_ABSENT
        log_warning(
            "uninstall",
            "Der markierte Apache-Live-Access-Log-Filter konnte nicht sicher "
            "zurückgebaut werden.",
        )
        return APACHE_SECURITY_FAILED
    if not _apache_security_entries_safe_for_removal():
        log_warning(
            "uninstall",
            "Apache-Laufzeitpfadschutz besitzt einen unerwarteten Root-Vertrag "
            "und wurde nicht verändert.",
        )
        return APACHE_SECURITY_FAILED

    remaining_paths = _remaining_apache_protected_webroot_paths()
    if remaining_paths:
        print(
            "  ℹ Apache-Sicherheitskonfiguration bleibt unangetastet, weil "
            "geschützte Webroot-Daten erhalten bleiben."
        )
        uninstall_logger.info(
            "Apache-Sicherheitskonfiguration wegen verbleibender Webroot-Daten "
            "beibehalten: %s",
            ", ".join(remaining_paths),
        )
        return APACHE_SECURITY_RETAINED

    if not remove_apache_live_access_log_filter(
        run_command,
        reload_apache=True,
    ):
        log_warning(
            "uninstall",
            "Der markierte Apache-Live-Access-Log-Filter konnte nicht sicher "
            "zurückgebaut werden.",
        )
        return APACHE_SECURITY_FAILED

    preflight = run_command(
        "sudo /usr/sbin/apache2ctl configtest",
        timeout=30,
    )
    if not preflight.get("success"):
        log_warning(
            "uninstall",
            "Apache-Konfiguration ist bereits vor dem Rückbau ungültig; "
            "Sicherheitskonfiguration bleibt unverändert.",
        )
        return APACHE_SECURITY_FAILED

    if os.path.lexists(enabled):
        disabled = run_command(
            "sudo a2disconf " + shlex.quote(CONF_NAME.removesuffix(".conf")),
            timeout=30,
        )
        if not disabled.get("success") or os.path.lexists(enabled):
            log_warning(
                "uninstall",
                "Apache-Laufzeitpfadschutz konnte nicht sicher deaktiviert werden.",
            )
            return APACHE_SECURITY_FAILED

    if os.path.lexists(destination):
        removed = run_command(
            "sudo rm -f -- " + shlex.quote(destination),
            timeout=30,
        )
        if not removed.get("success") or os.path.lexists(destination):
            log_warning(
                "uninstall",
                "Apache-Sicherheitskonfiguration konnte nicht sicher entfernt werden.",
            )
            return APACHE_SECURITY_FAILED

    postflight = run_command(
        "sudo /usr/sbin/apache2ctl configtest",
        timeout=30,
    )
    if not postflight.get("success"):
        log_warning(
            "uninstall",
            "Apache-Konfiguration ist nach dem Rückbau ungültig.",
        )
        return APACHE_SECURITY_FAILED
    reloaded = run_command("sudo systemctl reload apache2", timeout=30)
    if not reloaded.get("success"):
        log_warning(
            "uninstall",
            "Apache konnte nach dem Rückbau der Sicherheitskonfiguration "
            "nicht neu geladen werden.",
        )
        return APACHE_SECURITY_FAILED

    print("  ✓ Apache-Laufzeitpfadschutz deaktiviert und entfernt")
    return APACHE_SECURITY_REMOVED


def remove_manager_lock_boot_contract():
    """Entfernt nur den eindeutig produktverwalteten tmpfiles-Bootvertrag.

    Die Lockdateien unter ``/run`` bleiben bis zum nächsten Neustart bestehen.
    Dadurch verliert ein unerwartet noch laufender Manager niemals seinen
    benannten Lock-Inode; ``/run`` wird beim Boot ohnehin neu aufgebaut.
    """

    path = MANAGER_LOCK_TMPFILES_CONFIG
    if not os.path.lexists(path):
        return True
    try:
        info = os.lstat(path)
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        log_warning("uninstall", f"Manager-Lock-Bootvertrag ist nicht lesbar: {exc}")
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o644
        or info.st_nlink != 1
        or info.st_size > 16 * 1024
        or content != MANAGER_LOCK_TMPFILES_CONTENT
    ):
        log_warning(
            "uninstall",
            "Manager-Lock-Bootvertrag besitzt unerwartete Metadaten oder Inhalte "
            "und wurde nicht verändert.",
        )
        return False

    removed = run_command("sudo rm -f -- " + shlex.quote(path), timeout=30)
    if not removed.get("success") or os.path.lexists(path):
        log_warning(
            "uninstall",
            "Manager-Lock-Bootvertrag konnte nicht sicher entfernt werden.",
        )
        return False
    print("  ✓ Manager-Lock-Bootvertrag entfernt")
    print("  ℹ Laufzeit-Lockdateien bleiben als Schutz bis zum nächsten Neustart erhalten")
    return True


def uninstall_watchdog():
    """Entfernt Watchdog (Service, Skripte, Cron)."""
    print("\n→ Entferne Watchdog (Piguard)…")

    # Der Supervisor darf erst aus dem Bootvertrag verschwinden, wenn er
    # nachweislich gestoppt und deaktiviert ist. Sonst könnten parallel noch
    # Hardware-Writer neu gestartet werden.
    if _service_unit_exists("piguard"):
        for command in (
            "sudo systemctl stop piguard",
            "sudo systemctl disable piguard",
        ):
            result = run_command(command, timeout=10)
            if not isinstance(result, dict) or not result.get("success"):
                log_warning("uninstall", f"Watchdog-Schritt fehlgeschlagen: {command}")
                return False
        if not _service_is_proven_inactive("piguard") or not _service_is_proven_not_enabled("piguard"):
            log_warning("uninstall", "piguard ist nicht beweisbar inaktiv und deaktiviert.")
            return False
    service_path = "/etc/systemd/system/piguard.service"
    if os.path.lexists(service_path):
        info = os.lstat(service_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            log_warning("uninstall", "piguard-Unit ist kein sicherer regulärer Eintrag.")
            return False
        os.remove(service_path)
        reload_result = run_command("sudo systemctl daemon-reload", timeout=30)
        if not isinstance(reload_result, dict) or not reload_result.get("success"):
            log_warning("uninstall", "systemd-Reload nach Watchdog-Entfernung fehlgeschlagen.")
            return False
        print("  ✓ Service entfernt")

    # Skripte entfernen
    for f in ["/usr/local/bin/pi_guard.sh", "/usr/local/bin/boot_notify.sh"]:
        if os.path.exists(f):
            os.remove(f)
            print(f"  ✓ {f} gelöscht")

    # Cronjobs entfernen
    if remove_cron_pattern("boot_notify.sh"):
        print("  ✓ Cronjobs bereinigt")

    uninstall_logger.info("Watchdog deinstalliert.")
    log_task_completed("Deinstallation (Watchdog)")
    return True

def uninstall_ramdisk():
    """Entfernt RAM-Disk und Live-Grabber."""
    print("\n→ Entferne RAM-Disk & Live-Status…")
    install_user = get_install_user()

    # Service stoppen und entfernen
    run_command("sudo systemctl stop e3dc-grabber", timeout=10)
    run_command("sudo systemctl disable e3dc-grabber", timeout=10)
    if os.path.exists("/etc/systemd/system/e3dc-grabber.service"):
        os.remove("/etc/systemd/system/e3dc-grabber.service")
        run_command("sudo systemctl daemon-reload")
        print("  ✓ Service 'e3dc-grabber' entfernt")

    # Screen/Prozesse killen
    run_command(f"sudo -u {install_user} screen -S live-grabber -X quit", timeout=5)
    run_command(f"sudo -u {install_user} pkill -f get_live.sh", timeout=5)

    # Unmount
    run_command("sudo umount /var/www/html/ramdisk", timeout=5)

    # fstab bereinigen
    try:
        if os.path.exists("/etc/fstab"):
            with open("/etc/fstab", "r") as f:
                lines = f.readlines()
            with open("/etc/fstab", "w") as f:
                for line in lines:
                    if "/var/www/html/ramdisk" not in line:
                        f.write(line)
            run_command("sudo systemctl daemon-reload")
            print("  ✓ fstab bereinigt")
    except Exception as e:
        print(f"  ⚠ Fehler bei fstab: {e}")

    # Skript löschen
    grabber_script = os.path.join(get_home_dir(install_user), "get_live.sh")
    if os.path.exists(grabber_script):
        os.remove(grabber_script)
        print("  ✓ get_live.sh gelöscht")

    # Cronjobs
    remove_cron_pattern("get_live.sh")
    remove_cron_pattern("get_live_json.php")
    print("  ✓ Cronjobs bereinigt")

    uninstall_logger.info("RAM-Disk deinstalliert.")
    log_task_completed("Deinstallation (RAM-Disk)")

def uninstall_diagramm():
    """Entfernt Diagramm-Skripte und Webportal."""
    print("\n→ Entferne Diagramm-System & Webportal…")

    # Cronjobs
    remove_cron_pattern("plot_soc_changes.py")
    remove_cron_pattern("backup_history.php")

    # Verwaltete Web-Privilegien vollständig entfernen. Der Launcher bleibt
    # sonst auch nach der Webportal-Deinstallation als root-Aktor zurück.
    managed_privilege_files = [
        ("/etc/sudoers.d/010_e3dc_web_git", "Sudoers (git)"),
        ("/etc/sudoers.d/010_e3dc_web_update", "Sudoers (update)"),
        ("/etc/sudoers.d/020_e3dc_services", "Sudoers (Service-Launcher)"),
        ("/usr/local/sbin/e3dc-service-control", "root-eigener Service-Launcher"),
    ]
    for path, label in managed_privilege_files:
        if os.path.lexists(path):
            os.remove(path)
            print(f"  ✓ {label} entfernt")

    apache_security_cleanup = remove_apache_runtime_path_protection()

    # Python Skripte im Install-Ordner
    for f in ["plot_soc_changes.py", "plot_live_history.py"]:
        p = os.path.join(INSTALL_PATH, f)
        if os.path.exists(p):
            os.remove(p)
            print(f"  ✓ {f} gelöscht")

    uninstall_logger.info("Diagramm-System deinstalliert.")
    log_task_completed("Deinstallation (Diagramm)")
    return apache_security_cleanup

def uninstall_service():
    """Entfernt E3DC Systemd Service und Zusatz-Dienste."""
    print("\n→ Entferne E3DC-Control und Zusatz-Dienste…")
    install_user = get_install_user()

    # Stop & Disable aller E3DC-bezogenen Dienste
    services_to_remove = [
        "e3dc",
        "energy_manager",
        "e3dc-lux-live",
        "e3dc-ha",
        "e3dc-notifier",
        "e3dc-websocket",
        "e3dc-mqtt-hub",
        "e3dc-bluelink",
        "e3dc-weather-manager",
        "e3dc-forecast-evidence",
        "e3dc-storage-simulator",
        "e3dc-storage-manager",
        "e3dc-epex-manager",
        "e3dc-wallbox-manager",
        "e3dc-live",       # RSCP Python-Dienst
        "e3dc-heizstab",   # Heizstab/Shelly Manager (wp_type=2/3)
        "e3dc-climate-live", # Klimaanlage read-only Messdienst
        "e3dc-climate-control", # Klimaanlage Regel-Vorbereitung ohne aktive Kommandos
        "e3dc-idm-live",   # IDM Modbus Daemon (Legacy)
        "e3dc-stiebel-live", # Stiebel ISG Live Daemon
        "e3dc-dimplex-live", # Dimplex WPM Live Daemon
        "e3dc-matter-bridge", # Matter Bridge
    ]
    supervisors = ("piguard", "e3dc-watchdog", "e3dc-ha")
    quiesce_order = tuple(dict.fromkeys((*supervisors, *services_to_remove)))
    existing_services = tuple(
        service for service in quiesce_order if _service_unit_exists(service)
    )

    # Phase 1: erst alle Supervisoren und Writer stilllegen. Vor diesem
    # vollständigen Nachweis wird keine einzige Unit-Datei entfernt.
    for srv in existing_services:
        for command in (
            f"sudo systemctl stop {shlex.quote(srv)}",
            f"sudo systemctl disable {shlex.quote(srv)}",
        ):
            result = run_command(command, timeout=15)
            if not isinstance(result, dict) or not result.get("success"):
                log_warning("uninstall", f"Dienst konnte nicht stillgelegt werden: {srv}")
                return False
        if not _service_is_proven_inactive(srv) or not _service_is_proven_not_enabled(srv):
            log_warning(
                "uninstall",
                f"{srv} ist nicht beweisbar inaktiv und deaktiviert.",
            )
            return False

    for writer in HARDWARE_WRITER_SERVICE_NAMES:
        if not _service_is_proven_inactive(writer):
            log_warning("uninstall", f"Hardware-Writer bleibt aktiv: {writer}")
            return False

    unit_preimages = []
    for srv in services_to_remove:
        srv_file = f"/etc/systemd/system/{srv}.service"
        if not os.path.lexists(srv_file):
            continue
        info = os.lstat(srv_file)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            log_warning("uninstall", f"Unsicherer Unit-Eintrag bleibt erhalten: {srv_file}")
            return False
        unit_preimages.append((srv, srv_file, info.st_dev, info.st_ino))

    # Phase 2: nur die zuvor gebundenen Unit-Inodes entfernen.
    for srv, srv_file, expected_dev, expected_ino in unit_preimages:
        current = os.lstat(srv_file)
        if (current.st_dev, current.st_ino) != (expected_dev, expected_ino):
            log_warning("uninstall", f"Unit wechselte vor dem Entfernen: {srv_file}")
            return False
        os.remove(srv_file)
        if os.path.lexists(srv_file):
            return False
        print(f"  ✓ Service-Datei entfernt: {srv}.service")

    reload_result = run_command("sudo systemctl daemon-reload", timeout=30)
    if not isinstance(reload_result, dict) or not reload_result.get("success"):
        log_warning("uninstall", "systemd-Reload nach Unit-Entfernung fehlgeschlagen.")
        return False
    manager_lock_cleanup_ok = remove_manager_lock_boot_contract()
    if not manager_lock_cleanup_ok:
        return False

    # Screen killen
    run_command(f"sudo -u {install_user} screen -S E3DC -X quit", timeout=5)

    # Startskript weg
    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")
    if os.path.exists(sh_path):
        os.remove(sh_path)
        print("  ✓ E3DC.sh entfernt")

    # Legacy Cronjob entfernen (falls vorhanden)
    if remove_cron_pattern("E3DC.sh"):
        print("  ✓ Legacy Cronjob entfernt")

    cleanup_ok = bool(manager_lock_cleanup_ok)
    if cleanup_ok:
        uninstall_logger.info("E3DC und Zusatz-Services deinstalliert.")
        log_task_completed("Deinstallation (Services)")
    else:
        log_warning(
            "uninstall",
            "Service-Deinstallation besitzt einen offenen Manager-/Lock-Bootvertrag.",
        )
    return cleanup_ok


def uninstall_system_packages():
    """Entfernt die installierten System-Pakete."""
    print("\n→ Entferne System-Pakete…")

    packages = [
        "curl", "jq", "python3-bs4", "git", "screen",
        "apache2", "php", "php-curl", "python3-pip", "python3-venv",
        "python3-plotly", "libjpeg-dev", "zlib1g-dev",
        "libcurl4-openssl-dev", "libssl-dev",
        "libmosquitto-dev", "libjsoncpp-dev",
        "libsqlite3-dev", "build-essential", "cmake",
        "nodejs", "npm", "avahi-utils",
        "mosquitto", "mosquitto-clients"
    ]

    print("  → Folgende Pakete werden entfernt:")
    print("  " + ", ".join(packages))

    if input("\n  Fortfahren? (j/n): ").strip().lower() != 'j':
        print("→ Übersprungen.")
        return

    # Autoremove, um Abhängigkeiten zu bereinigen
    run_command("sudo apt-get -y autoremove --purge " + " ".join(packages), timeout=300)

    print("✓ System-Pakete entfernt.")
    uninstall_logger.info("System-Pakete deinstalliert.")
    log_task_completed("Deinstallation (System-Pakete)")


def uninstall_venv():
    """Entfernt das Python Virtual Environment."""
    try:
        install_user = get_install_user()
        account = pwd.getpwnam(install_user)
        venv_name, venv_path = resolve_venv_target(install_user)
    except Exception as exc:
        print(f"  ✗ venv-Ziel ist nicht vertrauensgebunden: {exc}")
        return False

    home_dir = os.path.realpath(account.pw_dir)
    normalized_venv = os.path.abspath(venv_path)
    if (
        os.path.dirname(normalized_venv) != home_dir
        or os.path.basename(normalized_venv) != venv_name
        or venv_name in {"", ".", ".."}
        or os.sep in venv_name
    ):
        print("  ✗ venv liegt nicht als direkter Child im gebundenen passwd-Home.")
        return False
    venv_path = normalized_venv

    print(f"\n→ Entferne Python venv ({venv_path})…")

    if os.path.lexists(venv_path):
        try:
            before = os.lstat(venv_path)
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISDIR(before.st_mode)
                or before.st_uid != account.pw_uid
            ):
                raise RuntimeError("venv ist kein benutzereigenes echtes Verzeichnis")
            current = os.lstat(venv_path)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError("venv wechselte vor der Löschung")
            result = run_command(
                "sudo -u "
                + shlex.quote(install_user)
                + " /usr/bin/rm -rf -- "
                + shlex.quote(venv_path),
                timeout=300,
            )
            if not isinstance(result, dict) or not result.get("success") or os.path.lexists(venv_path):
                raise RuntimeError("venv konnte unter der Installations-UID nicht entfernt werden")
            print(f"  ✓ {venv_path} gelöscht")
            uninstall_logger.info(f"venv entfernt: {venv_path}")
        except Exception as e:
            print(f"  ✗ Fehler beim Löschen: {e}")
            log_error("uninstall", f"Fehler beim Löschen von venv: {e}", e)
            return False
    else:
        print("  ℹ️  Kein venv gefunden.")

    log_task_completed("Deinstallation (venv)")
    return True


def uninstall_full():
    """Komplette Deinstallation."""
    print("\n=== Vollständige Deinstallation ===\n")
    print("ACHTUNG: Dieser Vorgang entfernt ALLE zugehörigen Komponenten,")
    print("inklusive Webportal, Datenbanken und System-Pakete.")

    if input("Wirklich ALLES entfernen? (j/n): ").strip().lower() != "j":
        return

    delete_data = False
    if input("\nMöchten Sie auch alle gesicherten Verlaufsdaten und Backups (Data-Ordner) dauerhaft löschen? (j/n): ").strip().lower() == "j":
        delete_data = True

    # Reihenfolge optimiert:
    if uninstall_watchdog() is not True:
        print("\n✗ Deinstallation abgebrochen: Watchdog/Supervisor blieb offen.\n")
        return False
    service_cleanup_ok = uninstall_service()
    if service_cleanup_ok is not True:
        print(
            "\n✗ Deinstallation abgebrochen: Mindestens ein Hardware-Writer "
            "ist nicht beweisbar inaktiv und deaktiviert.\n"
        )
        return False

    print("\n→ Beende und entferne Docker Container (falls vorhanden)…")
    if not _remove_managed_docker_container():
        print("  ✗ Docker-Container ist nicht beweisbar gestoppt und entfernt.")
        return False
    print("  ✓ Docker Container 'e3dc-control' entfernt")

    uninstall_ramdisk()

    # Webportal ohne Nachfrage entfernen (Data-Ordner ausnehmen, falls er nicht gelöscht werden soll)
    print("\n→ Entferne Webportal…")
    if not delete_data:
        # Lösche alles außer den data Ordner (z.B. history_backups, *.txt) im Webverzeichnis
        run_command("sudo find /var/www/html/ -mindepth 1 -maxdepth 1 ! -name 'data' ! -name 'history_backups' -exec rm -rf {} +", timeout=20)
        print("  ✓ Webverzeichnis geleert (Daten/Backups wurden behalten!)")
    else:
        run_command("sudo rm -rf /var/www/html/*", timeout=20)
        run_command(
            "sudo rm -rf /var/lib/e3dc-control/forecast-evidence",
            timeout=20,
        )
        print("  ✓ Webverzeichnis vollständig geleert")
        print("  ✓ Private Rohdaten der PV-Prognosediagnose gelöscht")

    apache_security_cleanup = uninstall_diagramm()
    if apache_security_cleanup == APACHE_SECURITY_FAILED:
        print("\n✗ Deinstallation wegen offenem Apache-Sicherheitsvertrag abgebrochen.\n")
        return False
    if uninstall_venv() is not True:
        print("\n✗ Deinstallation wegen unsicherem oder nicht löschbarem venv abgebrochen.\n")
        return False

    # System-Pakete deinstallieren
    uninstall_system_packages()

    # Config & Binary
    print("\n→ Programmdateien & Konfiguration:")
    if os.path.exists(INSTALL_PATH):
        if not delete_data:
            print("  ℹ️ Installationsordner ({}) wird wegen gewählter Datensicherung nicht komplett gelöscht.".format(INSTALL_PATH))
            # Optional nur gewisse Dateien löschen
        else:
            shutil.rmtree(INSTALL_PATH, ignore_errors=True)
            print("  ✓ Installationsordner gelöscht")

    if apache_security_cleanup == APACHE_SECURITY_RETAINED:
        print(
            "\n✓ Deinstallation abgeschlossen. Der Apache-Laufzeitpfadschutz "
            "bleibt für die bewusst erhaltenen Webroot-Daten aktiv.\n"
        )
    else:
        print("\n✓ Deinstallation abgeschlossen.\n")
    log_task_completed("Vollständige Deinstallation")
    return True


def uninstall_menu():
    """Menü für Deinstallation."""
    print("\n=== Deinstallation ===")
    print("1. Alles entfernen (Full Uninstall)")
    print("2. Nur Watchdog entfernen")
    print("3. Nur RAM-Disk & Live-Status entfernen")
    print("4. Nur Diagramm & Webportal entfernen")
    print("5. Nur E3DC-Service & Zusatz-Dienste entfernen")
    print("6. Nur Python venv entfernen")
    print("7. Abbrechen")

    choice = input("Auswahl: ").strip()

    if choice == "1": uninstall_full()
    elif choice == "2": uninstall_watchdog()
    elif choice == "3": uninstall_ramdisk()
    elif choice == "4": uninstall_diagramm()
    elif choice == "5": uninstall_service()
    elif choice == "6": uninstall_venv()
    else: print("Abbruch.")

register_command("29", "Deinstallation", uninstall_menu, sort_order=29)
