"""Fail-closed Apache-Schutz für Laufzeitdaten im Webroot."""

from __future__ import annotations

import os
import shlex
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    import pwd
except ImportError:  # pragma: no cover - Windows kann nur statisch prüfen.
    pwd = None


CONF_NAME = "e3dc-control-security.conf"
CONF_SOURCE = Path(__file__).resolve().parent / "apache" / CONF_NAME
CONF_DESTINATION = Path("/etc/apache2/conf-available") / CONF_NAME
CONF_ENABLED = Path("/etc/apache2/conf-enabled") / CONF_NAME
ACCESS_LOG_CONF_NAME = "e3dc-control-access-log.conf"
ACCESS_LOG_CONF_SOURCE = Path(__file__).resolve().parent / "apache" / ACCESS_LOG_CONF_NAME
ACCESS_LOG_CONF_DESTINATION = Path("/etc/apache2/conf-available") / ACCESS_LOG_CONF_NAME
DEFAULT_VHOST = Path("/etc/apache2/sites-available/000-default.conf")
LIVE_ACCESS_LOG_MARKER = (
    "# E3DC-Control: erfolgreiche Live-POSTs nicht persistent protokollieren"
)
LIVE_ACCESS_LOG_ORIGINAL = "CustomLog ${APACHE_LOG_DIR}/access.log combined"
LIVE_ACCESS_LOG_INCLUDE = (
    "IncludeOptional /etc/apache2/conf-available/e3dc-control-access-log.conf"
)
RUNTIME_PROBE_URLS = (
    "http://127.0.0.1/data/.e3dc-security-probe",
    "http://127.0.0.1/logs/.e3dc-security-probe",
    "http://127.0.0.1/ramdisk/.e3dc-security-probe",
    "http://127.0.0.1/tmp/.e3dc-security-probe",
    "http://127.0.0.1/history_backups/.e3dc-security-probe",
    "http://127.0.0.1/live_history.txt",
    "http://127.0.0.1/e3dc_paths.json",
    "http://127.0.0.1/e3dc.config.txt",
    "http://127.0.0.1/e3dc.strompreise.txt",
    "http://127.0.0.1/e3dc.wallbox.txt",
    "http://127.0.0.1/e3dc.wallbox.out",
)
LEGACY_HISTORY_PROBE_URL = (
    "http://127.0.0.1/history_backups/.e3dc-security-probe"
)
LEGACY_HISTORY_PATH = Path("/var/www/html/history_backups")


def _regular_file_bytes(
    path: Path,
    *,
    require_root_metadata: bool = False,
    reject_group_other_write: bool = False,
    maximum_bytes: int = 64 * 1024,
) -> bytes | None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > max(1, int(maximum_bytes))
            or metadata.st_nlink != 1
        ):
            return None
        if require_root_metadata and (
            metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o644
        ):
            return None
        if reject_group_other_write and metadata.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            return None
        return path.read_bytes()
    except OSError:
        return None


def _enabled_entry_is_direct_root_link() -> bool:
    """Akzeptiert nur einen direkten root-eigenen Link auf die Zielkonfiguration."""

    try:
        metadata = CONF_ENABLED.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
        ):
            return False
        raw_target = Path(os.readlink(CONF_ENABLED))
        direct_target = (
            raw_target
            if raw_target.is_absolute()
            else CONF_ENABLED.parent / raw_target
        )
        return os.path.abspath(direct_target) == os.path.abspath(CONF_DESTINATION)
    except OSError:
        return False


def _enabled_entry_safe_for_mutation() -> bool:
    try:
        CONF_ENABLED.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return _enabled_entry_is_direct_root_link()


def _trusted_source_payload(path: Path) -> bytes | None:
    """Bindet nur eine Quelle außerhalb der Schreibhoheit des Webservers."""

    try:
        if pwd is None:
            return None
        web_uid = pwd.getpwnam("www-data").pw_uid
        for directory in (
            path.parent,
            path.parent.parent,
            path.parent.parent.parent,
        ):
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid == web_uid
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return None
        source_metadata = path.lstat()
        if source_metadata.st_uid == web_uid:
            return None
    except (KeyError, OSError):
        return None
    return _regular_file_bytes(
        path,
        reject_group_other_write=True,
    )


def _source_payload() -> bytes | None:
    return _trusted_source_payload(CONF_SOURCE)


def _access_log_source_payload() -> bytes | None:
    return _trusted_source_payload(ACCESS_LOG_CONF_SOURCE)


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def render_live_access_log_vhost(payload: bytes, *, enabled: bool) -> bytes | None:
    """Schaltet den exakt gebundenen Default-vHost verlustfrei um.

    Fremde oder bereits manuell angepasste ``CustomLog``-Verträge werden nicht
    geraten. Der Rückweg akzeptiert ausschließlich den eigenen markierten
    Zweizeiler und stellt danach die ursprüngliche Debian-Direktive wieder her.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines(keepends=True)
    marker_indices = [
        index for index, line in enumerate(lines)
        if line.strip() == LIVE_ACCESS_LOG_MARKER
    ]
    include_indices = [
        index for index, line in enumerate(lines)
        if line.strip() == LIVE_ACCESS_LOG_INCLUDE
    ]
    original_indices = [
        index for index, line in enumerate(lines)
        if line.strip() == LIVE_ACCESS_LOG_ORIGINAL
    ]
    direct_access_log_indices = [
        index for index, line in enumerate(lines)
        if line.strip().startswith(
            "CustomLog ${APACHE_LOG_DIR}/access.log "
        )
    ]

    managed = bool(
        len(marker_indices) == 1
        and len(include_indices) == 1
        and include_indices[0] == marker_indices[0] + 1
        and _leading_whitespace(lines[marker_indices[0]])
        == _leading_whitespace(lines[include_indices[0]])
    )
    if enabled:
        if managed and not original_indices and not direct_access_log_indices:
            return payload
        if (
            marker_indices
            or include_indices
            or len(original_indices) != 1
            or direct_access_log_indices != original_indices
        ):
            return None
        index = original_indices[0]
        line = lines[index]
        indent = _leading_whitespace(line)
        ending = _line_ending(line)
        lines[index:index + 1] = [
            indent + LIVE_ACCESS_LOG_MARKER + (ending or "\n"),
            indent + LIVE_ACCESS_LOG_INCLUDE + ending,
        ]
    else:
        if not marker_indices and not include_indices:
            return (
                payload
                if len(original_indices) == 1
                and direct_access_log_indices == original_indices
                else None
            )
        if not managed or original_indices or direct_access_log_indices:
            return None
        marker_index = marker_indices[0]
        marker_line = lines[marker_index]
        indent = _leading_whitespace(marker_line)
        ending = _line_ending(lines[include_indices[0]]) or _line_ending(marker_line)
        lines[marker_index:include_indices[0] + 1] = [
            indent + LIVE_ACCESS_LOG_ORIGINAL + ending,
        ]
    return "".join(lines).encode("utf-8")


def _install_root_payload(run_command, source: Path, destination: Path) -> bool:
    result = run_command(
        "sudo install -o root -g root -m 0644 "
        + shlex.quote(str(source))
        + " "
        + shlex.quote(str(destination)),
        timeout=30,
    )
    return bool(result.get("success"))


def _install_root_bytes(run_command, payload: bytes, destination: Path) -> bool:
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="e3dc-apache-", delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = handle.name
        return _install_root_payload(run_command, Path(temporary_path), destination)
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def apache_live_access_log_filter_configured() -> bool:
    source_payload = _access_log_source_payload()
    if source_payload is None:
        return False
    installed_payload = _regular_file_bytes(
        ACCESS_LOG_CONF_DESTINATION,
        require_root_metadata=True,
    )
    vhost_payload = _regular_file_bytes(
        DEFAULT_VHOST,
        require_root_metadata=True,
        maximum_bytes=256 * 1024,
    )
    if installed_payload != source_payload or vhost_payload is None:
        return False
    return render_live_access_log_vhost(vhost_payload, enabled=True) == vhost_payload


def ensure_apache_live_access_log_filter(
    run_command,
    *,
    reload_apache: bool = True,
    allow_mutation: bool = True,
) -> bool:
    """Installiert den schmalen Access-Log-Filter mit geprüftem Rückweg."""

    source_payload = _access_log_source_payload()
    vhost_before = _regular_file_bytes(
        DEFAULT_VHOST,
        require_root_metadata=True,
        maximum_bytes=256 * 1024,
    )
    if source_payload is None or vhost_before is None:
        return False
    vhost_after = render_live_access_log_vhost(vhost_before, enabled=True)
    if vhost_after is None:
        return False
    already_configured = apache_live_access_log_filter_configured()
    if already_configured:
        return True
    if not allow_mutation:
        return False

    access_before = _regular_file_bytes(
        ACCESS_LOG_CONF_DESTINATION,
        require_root_metadata=True,
    )
    if os.path.lexists(ACCESS_LOG_CONF_DESTINATION) and access_before is None:
        return False
    preflight = run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
    if not preflight.get("success"):
        return False

    def rollback() -> bool:
        restored_vhost = _install_root_bytes(run_command, vhost_before, DEFAULT_VHOST)
        if access_before is None:
            removed = run_command(
                "sudo rm -f -- " + shlex.quote(str(ACCESS_LOG_CONF_DESTINATION)),
                timeout=30,
            )
            restored_access = bool(removed.get("success"))
        else:
            restored_access = _install_root_bytes(
                run_command,
                access_before,
                ACCESS_LOG_CONF_DESTINATION,
            )
        syntax = run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
        if reload_apache and syntax.get("success"):
            reloaded = run_command("sudo systemctl reload apache2", timeout=30)
        else:
            reloaded = {"success": True}
        return bool(
            restored_vhost
            and restored_access
            and syntax.get("success")
            and reloaded.get("success")
        )

    if not _install_root_payload(
        run_command,
        ACCESS_LOG_CONF_SOURCE,
        ACCESS_LOG_CONF_DESTINATION,
    ):
        rollback()
        return False
    if vhost_after != vhost_before and not _install_root_bytes(
        run_command,
        vhost_after,
        DEFAULT_VHOST,
    ):
        rollback()
        return False
    configtest = run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
    if not configtest.get("success"):
        rollback()
        return False
    if reload_apache:
        reloaded = run_command("sudo systemctl reload apache2", timeout=30)
        if not reloaded.get("success"):
            rollback()
            return False
    if not apache_live_access_log_filter_configured():
        rollback()
        return False
    return True


def remove_apache_live_access_log_filter(
    run_command,
    *,
    reload_apache: bool = True,
) -> bool:
    """Entfernt ausschließlich den eigenen markierten vHost-Vertrag."""

    vhost_before = _regular_file_bytes(
        DEFAULT_VHOST,
        require_root_metadata=True,
        maximum_bytes=256 * 1024,
    )
    if vhost_before is None:
        return bool(
            not os.path.lexists(DEFAULT_VHOST)
            and not os.path.lexists(ACCESS_LOG_CONF_DESTINATION)
        )
    vhost_after = render_live_access_log_vhost(vhost_before, enabled=False)
    if vhost_after is None:
        return False
    access_before = _regular_file_bytes(
        ACCESS_LOG_CONF_DESTINATION,
        require_root_metadata=True,
    )
    if os.path.lexists(ACCESS_LOG_CONF_DESTINATION) and access_before is None:
        return False
    if vhost_after == vhost_before and access_before is None:
        return True

    preflight = run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
    if not preflight.get("success"):
        return False
    if vhost_after != vhost_before and not _install_root_bytes(
        run_command,
        vhost_after,
        DEFAULT_VHOST,
    ):
        _install_root_bytes(run_command, vhost_before, DEFAULT_VHOST)
        return False
    removed = run_command(
        "sudo rm -f -- " + shlex.quote(str(ACCESS_LOG_CONF_DESTINATION)),
        timeout=30,
    )
    configtest = run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
    if not (removed.get("success") and configtest.get("success")):
        _install_root_bytes(run_command, vhost_before, DEFAULT_VHOST)
        if access_before is not None:
            _install_root_bytes(
                run_command,
                access_before,
                ACCESS_LOG_CONF_DESTINATION,
            )
        run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
        return False
    if reload_apache:
        reloaded = run_command("sudo systemctl reload apache2", timeout=30)
        if not reloaded.get("success"):
            _install_root_bytes(run_command, vhost_before, DEFAULT_VHOST)
            if access_before is not None:
                _install_root_bytes(
                    run_command,
                    access_before,
                    ACCESS_LOG_CONF_DESTINATION,
                )
            run_command("sudo /usr/sbin/apache2ctl configtest", timeout=30)
            run_command("sudo systemctl reload apache2", timeout=30)
            return False
    return bool(
        render_live_access_log_vhost(
            _regular_file_bytes(
                DEFAULT_VHOST,
                require_root_metadata=True,
                maximum_bytes=256 * 1024,
            ) or b"",
            enabled=False,
        )
        is not None
        and not os.path.lexists(ACCESS_LOG_CONF_DESTINATION)
    )


def _already_enabled(source_payload: bytes) -> bool:
    destination_payload = _regular_file_bytes(
        CONF_DESTINATION,
        require_root_metadata=True,
    )
    if destination_payload != source_payload:
        return False
    return _enabled_entry_is_direct_root_link()


def apache_runtime_paths_protected() -> bool:
    """Belegt alle Laufzeitsperren gegen die tatsächlich laufende Generation."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in RUNTIME_PROBE_URLS:
        request = urllib.request.Request(url, method="HEAD")
        try:
            with opener.open(request, timeout=5) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
        except (OSError, urllib.error.URLError, ValueError):
            return False
        if (
            url == LEGACY_HISTORY_PROBE_URL
            and status == 404
            and not os.path.lexists(LEGACY_HISTORY_PATH)
        ):
            # Auf aktuellen Installationen existiert dieser historische
            # DocumentRoot-Pfad nicht mehr. Ein echtes HTTP 404 belegt dann,
            # dass gegenwärtig keine Datei ausgeliefert wird; sobald der Pfad
            # existiert, bleibt ausschließlich HTTP 403 zulässig.
            continue
        if status != 403:
            return False
    return True


def ensure_apache_runtime_path_protection(
    run_command,
    *,
    reload_apache: bool = True,
    allow_mutation: bool = True,
) -> bool:
    """Installiert die root-eigene Sperre und lädt Apache nur bei Drift neu."""

    source_payload = _source_payload()
    if source_payload is None:
        return False
    already_enabled = _already_enabled(source_payload)
    if os.path.exists("/.dockerenv"):
        return bool(
            already_enabled
            and apache_live_access_log_filter_configured()
            and apache_runtime_paths_protected()
        )
    if not already_enabled and not allow_mutation:
        return False

    if not already_enabled:
        if not _enabled_entry_safe_for_mutation():
            return False
        install_result = run_command(
            "sudo install -o root -g root -m 0644 "
            + shlex.quote(str(CONF_SOURCE))
            + " "
            + shlex.quote(str(CONF_DESTINATION)),
            timeout=30,
        )
        enable_result = run_command("sudo a2enconf e3dc-control-security", timeout=30)
        if not (install_result.get("success") and enable_result.get("success")):
            return False

    if not ensure_apache_live_access_log_filter(
        run_command,
        reload_apache=False,
        allow_mutation=allow_mutation,
    ):
        return False

    configtest_result = run_command(
        "sudo /usr/sbin/apache2ctl configtest",
        timeout=30,
    )
    if not configtest_result.get("success"):
        return False
    if reload_apache:
        reload_result = run_command("sudo systemctl reload apache2", timeout=30)
        if not reload_result.get("success"):
            return False
    return bool(
        _already_enabled(source_payload)
        and apache_live_access_log_filter_configured()
        and (not reload_apache or apache_runtime_paths_protected())
    )
