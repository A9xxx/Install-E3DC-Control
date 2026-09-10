#!/usr/bin/env python3
"""Startet Docker-EMS-Prozesse mit fester Identität und ohne Privilegien."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import stat
import sys

IDENTITY = "/usr/local/lib/e3dc-control/docker_runtime_identity.py"
LAUNCHER = "/usr/local/bin/e3dc-docker-runtime"
PYTHON = "/opt/venv/bin/python3"
LOG_DIRECTORY = "/var/www/html/logs"


def _identity():
    # Kein Import aus schreibbaren Arbeits- oder Datenverzeichnissen.
    path = Path(IDENTITY)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Unsicherer Identitätsmodulpfad")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise RuntimeError("Unsicheres Identitätsmodul")
    spec = importlib.util.spec_from_file_location("e3dc_docker_runtime_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open_log(name: str) -> int:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.log", name):
        raise RuntimeError("Ungültiger Produktlogname")
    directory = os.open(LOG_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid not in (0, 33) or info.st_gid != 33 or info.st_mode & 0o002:
            raise RuntimeError("Unsicheres Produktlogverzeichnis")
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        created = False
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            created = True
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in (0, 33, 991) or info.st_gid not in (33, 991) or info.st_mode & 0o002:
                raise RuntimeError("Unsichere Produktlogdatei")
            if created:
                os.fchown(fd, -1, 33)
                os.fchmod(fd, 0o660)
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dropped", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or command[0] != PYTHON:
        raise RuntimeError("EMS-Start benötigt den festen Python-Interpreter")
    identity = _identity()
    identity.validate_runtime_account()
    identity.validate_docker_product_binding()
    if not args.dropped:
        if os.getresuid() != (0, 0, 0):
            raise RuntimeError("Der privilegierte EMS-Start benötigt den Container-Bootstrap")
        forwarded = (["--log", args.log] if args.log else []) + ["--"] + command
        os.execv("/usr/bin/setpriv", [
            "/usr/bin/setpriv", "--reuid=991", "--regid=991", "--groups=991,33",
            "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "--no-new-privs",
            PYTHON, "-I", "-B", LAUNCHER, "--dropped", *forwarded,
        ])
    if not identity.is_docker_runtime_process():
        raise RuntimeError("EMS-Laufzeitidentität oder Privilegienabgabe ungültig")
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    environment.update(HOME=identity.RUNTIME_HOME, USER=identity.RUNTIME_USER,
                       LOGNAME=identity.RUNTIME_USER, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    os.umask(0o007)
    if args.log:
        fd = _open_log(args.log)
        try:
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            if fd > 2:
                os.close(fd)
    os.execve(PYTHON, command, environment)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"EMS-Start abgebrochen: {exc}", file=sys.stderr)
        raise SystemExit(1)
