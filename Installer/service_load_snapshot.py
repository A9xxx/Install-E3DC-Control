#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only service load snapshot for support diagnostics.

This tool intentionally does not read configuration files.  It only reports
known service metadata plus process counters from systemd and /proc.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Callable, Dict, Iterable, Optional

try:
    from service_catalog import ServiceModule, iter_modules, service_load_profile
except Exception:  # pragma: no cover - package import fallback
    from .service_catalog import ServiceModule, iter_modules, service_load_profile  # type: ignore


SystemctlRunner = Callable[[str], Dict[str, Any]]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return int(default)


def systemctl_show(unit: str) -> Dict[str, Any]:
    if not unit:
        return {}
    try:
        proc = subprocess.run(
            ["systemctl", "show", unit, "--property=MainPID,ActiveState,SubState,Description", "--no-page"],
            text=True,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except Exception as exc:
        return {"error": exc.__class__.__name__}
    result: Dict[str, Any] = {"returncode": proc.returncode}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    if proc.stderr.strip():
        result["stderr"] = proc.stderr.strip()[:160]
    return result


def read_proc_metrics(pid: int, *, proc_root: str = "/proc") -> Dict[str, Any]:
    pid = int(pid or 0)
    if pid <= 0:
        return {}
    base = os.path.join(proc_root, str(pid))
    metrics: Dict[str, Any] = {"pid": pid}
    try:
        with open(os.path.join(base, "stat"), "r", encoding="utf-8", errors="ignore") as handle:
            stat = handle.read().split()
        if len(stat) > 15:
            metrics["cpu_user_jiffies"] = _safe_int(stat[13], 0)
            metrics["cpu_system_jiffies"] = _safe_int(stat[14], 0)
    except Exception:
        pass
    try:
        with open(os.path.join(base, "status"), "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    metrics["rss_kb"] = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
                elif line.startswith("Threads:"):
                    parts = line.split()
                    metrics["threads"] = _safe_int(parts[1] if len(parts) > 1 else 0, 0)
    except Exception:
        pass
    try:
        with open(os.path.join(base, "io"), "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                if key in ("read_bytes", "write_bytes"):
                    metrics[key] = _safe_int(value, 0)
    except Exception:
        pass
    return metrics


def service_load_entry(
    module: ServiceModule,
    *,
    proc_root: str = "/proc",
    systemctl_runner: Optional[SystemctlRunner] = None,
) -> Dict[str, Any]:
    runner = systemctl_runner or systemctl_show
    unit = module.service_unit or ""
    systemd = runner(unit) if unit else {}
    pid = _safe_int(systemd.get("MainPID"), 0)
    entry = {
        "key": module.key,
        "display_name": module.display_name,
        "service_unit": unit,
        "group": module.group,
        "optional": bool(module.optional),
        "load_profile": service_load_profile(module),
        "active_state": systemd.get("ActiveState", ""),
        "sub_state": systemd.get("SubState", ""),
        "pid": pid,
        "proc": read_proc_metrics(pid, proc_root=proc_root),
    }
    if systemd.get("error"):
        entry["systemctl_error"] = systemd.get("error")
    return entry


def build_service_load_snapshot(
    *,
    modules: Optional[Iterable[ServiceModule]] = None,
    proc_root: str = "/proc",
    systemctl_runner: Optional[SystemctlRunner] = None,
) -> Dict[str, Any]:
    module_list = list(modules if modules is not None else iter_modules(include_optional=True))
    services = [
        service_load_entry(module, proc_root=proc_root, systemctl_runner=systemctl_runner)
        for module in module_list
        if module.service_unit
    ]
    summary: Dict[str, int] = {}
    for service in services:
        profile = str(service.get("load_profile") or "unknown")
        summary[profile] = summary.get(profile, 0) + 1
    return {
        "ts": int(time.time()),
        "source": "service_load_snapshot",
        "privacy": "no_config_or_tokens_read",
        "summary": summary,
        "services": services,
    }


def main() -> int:
    print(json.dumps(build_service_load_snapshot(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
