#!/usr/bin/env python3
"""Read-only boot sanity checks for installer/update exits."""

from __future__ import annotations

import os
import subprocess

try:
    from .utils import run_command
except Exception:  # pragma: no cover - direct execution fallback
    def run_command(cmd, timeout=10):
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except Exception as exc:
            return {"success": False, "stdout": "", "stderr": str(exc), "returncode": -1}


INIT_PATH = "/sbin/init"
SYSTEMD_CANDIDATES = ("/lib/systemd/systemd", "/usr/lib/systemd/systemd")


def _cmd_ok(cmd: str, timeout: int = 10) -> bool:
    result = run_command(cmd, timeout=timeout)
    return bool(result.get("success"))


def _cmd_output(cmd: str, timeout: int = 10) -> str:
    result = run_command(cmd, timeout=timeout)
    return (result.get("stdout", "") + result.get("stderr", "")).strip()


def _append_issue(issues: list[str], message: str) -> None:
    if message not in issues:
        issues.append(message)


def check_boot_sanity(verbose: bool = True) -> bool:
    """Check boot-critical files and fstab without changing the system."""
    issues: list[str] = []

    if not os.path.exists(INIT_PATH):
        _append_issue(issues, f"{INIT_PATH} fehlt")
    elif not os.access(INIT_PATH, os.X_OK):
        _append_issue(issues, f"{INIT_PATH} ist nicht ausfuehrbar")

    if not any(os.path.exists(path) for path in SYSTEMD_CANDIDATES):
        _append_issue(issues, "systemd-Binary fehlt (/lib/systemd/systemd oder /usr/lib/systemd/systemd)")
    elif not any(os.path.exists(path) and os.access(path, os.X_OK) for path in SYSTEMD_CANDIDATES):
        _append_issue(issues, "systemd-Binary ist nicht ausfuehrbar")

    if _cmd_ok("command -v findmnt >/dev/null 2>&1", timeout=5):
        root_mount = _cmd_output("findmnt -n -o SOURCE,FSTYPE,OPTIONS /", timeout=10)
        if not root_mount:
            _append_issue(issues, "Root-Dateisystem konnte mit findmnt nicht gelesen werden")
        fstab_verify = run_command("findmnt --verify --tab-file /etc/fstab", timeout=20)
        if not fstab_verify.get("success"):
            details = (fstab_verify.get("stderr") or fstab_verify.get("stdout") or "").strip()
            _append_issue(issues, "fstab-Validierung fehlgeschlagen" + (f": {details}" if details else ""))

    audit = _cmd_output("dpkg --audit", timeout=20)
    if audit:
        _append_issue(issues, "dpkg meldet unvollstaendige Paketinstallation")

    if verbose:
        print("\n=== Boot-Sanitycheck ===")
        file_info = _cmd_output(
            "if command -v file >/dev/null 2>&1; then file -L /sbin/init /lib/systemd/systemd /usr/lib/systemd/systemd 2>/dev/null; fi",
            timeout=10,
        )
        if file_info:
            for line in file_info.splitlines():
                print(f"  {line}")
        if _cmd_ok("command -v findmnt >/dev/null 2>&1", timeout=5):
            root_mount = _cmd_output("findmnt -n -o SOURCE,FSTYPE,OPTIONS /", timeout=10)
            if root_mount:
                print(f"  Root-Mount: {root_mount}")
        if not issues:
            print("  [OK] Boot-kritische Dateien und fstab wirken plausibel.")
        else:
            print("  [WARNUNG] Boot-kritische Auffaelligkeiten gefunden:")
            for issue in issues:
                print(f"    - {issue}")
            print("  Bitte vor einem Reboot pruefen: /sbin/init, systemd, dpkg --audit, /etc/fstab und fsck von einem zweiten Linux-System.")

    return not issues


if __name__ == "__main__":
    raise SystemExit(0 if check_boot_sanity(verbose=True) else 1)
