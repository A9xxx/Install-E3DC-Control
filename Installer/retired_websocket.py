"""Stilllegung des bis 5.5.5 ausgelieferten E3DC-WebSocket-Dienstes."""

import os
from pathlib import Path
import re
import subprocess
import tempfile


RETIRED_UNIT = "e3dc-websocket.service"
APACHE_SITE = Path("/etc/apache2/sites-available/000-default.conf")
UNIT_FILE = Path("/etc/systemd/system") / RETIRED_UNIT


def _result(run, argv, *, status_codes=(0,)):
    """Befehlsfehler bleiben negative Ergebnisse, auch beim einfachen Runner."""
    try:
        result = run(argv, timeout=30)
        if isinstance(result, dict):
            code = result.get("returncode")
            ok = bool(result.get("success")) if code is None else code in status_codes
            return ok, str(result.get("stdout") or "")
        return result.returncode in status_codes, str(result.stdout or "")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False, ""


def _render_without_proxy(content):
    """Entfernt nur die ausgelieferte Loopback-Regel; fremde Ziele bleiben."""
    owned = re.compile(
        r'^\s*ProxyPass(?:Reverse)?\s+["\']?/ws/?["\']?\s+'
        r'["\']?ws://(?:127\.0\.0\.1|localhost):8765/["\']?\s*(?:#.*)?$'
    )
    return "".join(line for line in content.splitlines(keepends=True)
                   if not owned.fullmatch(line.rstrip("\r\n")))


def _has_proxy(content):
    return bool(re.search(r'(?mi)^\s*ProxyPass(?:Reverse)?\s+["\']?/ws/?["\']?\s', content))


def remove_proxy(run, conf_path=None):
    """Ändert die gesicherte Site ohne Reload und prüft vor ihrer Aktivierung."""
    path = Path(conf_path or APACHE_SITE)
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        return ["WARNUNG: Apache-Site nicht lesbar; die alte /ws-Regel wurde nicht entfernt."]
    try:
        content = original.decode("utf-8")
        rendered = _render_without_proxy(content).encode("utf-8")
    except UnicodeError:
        return ["WARNUNG: Apache-Site nicht als UTF-8 lesbar; /ws bitte prüfen."]
    if rendered == original:
        return (["WARNUNG: Fremde /ws-Proxyregel bleibt erhalten; den Anschluss bitte manuell schließen."]
                if _has_proxy(content) else [])

    def write(payload):
        staged = str(path) + ".e3dc-retire.tmp"
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(payload)
            tmp.flush()
            try:
                ok, _ = _result(run, ["sudo", "install", "-o", "root", "-g", "root", "-m", "0644", "--", tmp.name, staged])
                if ok:
                    ok, _ = _result(run, ["sudo", "mv", "-f", "--", staged, str(path)])
            finally:
                cleaned, _ = _result(run, ["sudo", "rm", "-f", "--", staged])
            return ok and cleaned and path.read_bytes() == payload

    def restore_warning(reason):
        try:
            write(original)
            restored = path.read_bytes() == original
        except (OSError, RuntimeError):
            restored = False
        return ["WARNUNG: " + reason + "; "
                + ("bisherige Site wiederhergestellt, /ws bleibt offen."
                   if restored else "Wiederherstellung der bisherigen Site unbestätigt; Apache-Konfiguration bitte reparieren.")]

    try:
        if not write(rendered):
            return restore_warning("Alte /ws-Proxyregel konnte nicht sicher entfernt werden")
        ok, _ = _result(run, ["sudo", "/usr/sbin/apache2ctl", "configtest"])
        if not ok:
            return restore_warning("Apache-Prüfung nach /ws-Abbau fehlgeschlagen")
        if _has_proxy(path.read_text(encoding="utf-8")):
            return ["WARNUNG: Fremde /ws-Proxyregel bleibt erhalten; Anschluss bitte manuell schließen."]
    except (OSError, RuntimeError, UnicodeError) as exc:
        return restore_warning(f"/ws-Abbau nicht bestätigt ({type(exc).__name__})")
    print("[OK] Alter E3DC-WebSocket-Proxy entfernt; Apache-Konfiguration gültig.")
    return []


def retire_service(run, unit_path=None):
    """Nach bestätigtem Zielstart entfernen; vor diesem Punkt bleibt Rückfall möglich."""
    warning = ["WARNUNG: Alt-Dienst konnte nicht sicher stillgelegt werden, wird beim nächsten Update erneut versucht"]
    try:
        ok, state = _result(run, ["systemctl", "show", RETIRED_UNIT, "--property=LoadState", "--value"])
        path = Path(unit_path or UNIT_FILE)
        if not ok or not state.strip() or state.strip() == "unknown":
            return warning
        if state.strip() == "not-found" and not os.path.lexists(path):
            return []
        stopped, _ = _result(run, ["sudo", "systemctl", "stop", RETIRED_UNIT])
        disabled, _ = _result(run, ["sudo", "systemctl", "disable", RETIRED_UNIT])
        if not stopped or not disabled:
            return warning
        # systemctl verwendet für inaktive/deaktivierte Zustände regulär Exit 3/1.
        active_ok, active = _result(run, ["systemctl", "is-active", RETIRED_UNIT], status_codes=(0, 3))
        enabled_ok, enabled = _result(run, ["systemctl", "is-enabled", RETIRED_UNIT], status_codes=(0, 1, 4))
        inactive = active_ok and active.strip() in {"inactive", "failed"}
        if active_ok and active.strip() == "not-found":
            load_ok, load = _result(run, ["systemctl", "show", RETIRED_UNIT, "--property=LoadState", "--value"])
            inactive = load_ok and load.strip() == "not-found"
        if not inactive or not enabled_ok or enabled.strip() not in {"disabled", "not-found", "masked"}:
            return warning
        removed, _ = _result(run, ["sudo", "rm", "-f", "--", str(path)])
        reloaded, _ = _result(run, ["sudo", "systemctl", "daemon-reload"])
        loaded_ok, loaded = _result(run, ["systemctl", "show", RETIRED_UNIT, "--property=LoadState", "--value"])
        if not removed or not reloaded or not loaded_ok or os.path.lexists(path) or loaded.strip() != "not-found":
            return warning
    except (OSError, RuntimeError):
        return warning
    print("[OK] Alter E3DC-WebSocket-Dienst gestoppt, deaktiviert und entfernt. Ersatz ist MQTT.")
    return []
