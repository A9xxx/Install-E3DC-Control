#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Signal an Watchtower für den Imagewechsel des E3DC-Control-Containers.

Der Container steuert den Docker-Daemon nicht selbst. Er gibt dem vom Nutzer
eingerichteten Watchtower über dessen lokale HTTP-API nur das Signal, das
eigene Image zu prüfen und bei einer neuen Version den Container neu zu
erstellen. Ohne konfiguriertes Token oder ohne laufenden Watchtower passiert
nichts; der Aufrufer zeigt dann die Host-Befehle.

Aufruf: ``docker_watchtower_client.py trigger --reason webui|schedule``
liefert genau eine JSON-Zeile mit ``success``, ``status`` und ``message``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

OFFICIAL_IMAGE_REPOSITORY = "ghcr.io/a9xxx/install-e3dc-control"
URL_ENVIRONMENT_KEY = "E3DC_WATCHTOWER_API_URL"
TOKEN_ENVIRONMENT_KEY = "E3DC_WATCHTOWER_API_TOKEN"
DEFAULT_TIMEOUT_S = 15.0
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{16,512}")
_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+(?::\d{1,5})?$")

STATUS_MESSAGES = {
    "accepted": "Watchtower prüft jetzt das Image und erstellt den Container bei einer neuen Version neu.",
    "not_configured": "Watchtower ist für diese Installation nicht eingerichtet.",
    "unreachable": "Watchtower ist nicht erreichbar; das Profil auto-update läuft nicht.",
    "unauthorized": "Watchtower lehnt das Token ab; E3DC_WATCHTOWER_API_TOKEN in .env für beide Dienste prüfen.",
    "busy": "Watchtower führt bereits ein Update aus.",
    "error": "Watchtower meldete einen unerwarteten Fehler.",
}


def watchtower_configuration(environ=None) -> dict:
    """Liest URL und Token nur aus der Containerumgebung, nie aus Nutzerdaten."""
    source = os.environ if environ is None else environ
    url = str(source.get(URL_ENVIRONMENT_KEY) or "").strip().rstrip("/")
    token = str(source.get(TOKEN_ENVIRONMENT_KEY) or "").strip()
    url_ok = bool(url) and _URL_PATTERN.fullmatch(url) is not None
    token_ok = bool(token) and _TOKEN_PATTERN.fullmatch(token) is not None
    return {
        "url": url if url_ok else "",
        "token": token if token_ok else "",
        "configured": url_ok and token_ok,
    }


def _result(status: str, *, http_status: int = 0, detail: str = "") -> dict:
    message = STATUS_MESSAGES.get(status, STATUS_MESSAGES["error"])
    if detail:
        message = f"{message} ({detail})"
    return {
        "success": status == "accepted",
        "status": status,
        "http_status": int(http_status),
        "message": message,
    }


def trigger_update(reason: str = "webui", *, environ=None, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Löst genau einen asynchronen Update-Lauf für das eigene Image aus."""
    configuration = watchtower_configuration(environ)
    if not configuration["configured"]:
        return _result("not_configured")
    query = urllib.parse.urlencode({"image": OFFICIAL_IMAGE_REPOSITORY, "async": "true"})
    request = urllib.request.Request(
        f"{configuration['url']}/v1/update?{query}",
        method="POST",
        data=b"",
        headers={
            "Authorization": f"Bearer {configuration['token']}",
            "User-Agent": f"e3dc-control-update/{reason}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _result("accepted", http_status=response.status)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result("unauthorized", http_status=exc.code)
        if exc.code == 429:
            return _result("busy", http_status=exc.code)
        return _result("error", http_status=exc.code, detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason_text = getattr(exc, "reason", None) or exc
        return _result("unreachable", detail=str(reason_text)[:120])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    trigger = subparsers.add_parser("trigger", help="Update-Lauf bei Watchtower anstoßen")
    trigger.add_argument("--reason", default="webui", choices=("webui", "schedule"))
    trigger.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    subparsers.add_parser("status", help="Nur prüfen, ob URL und Token konfiguriert sind")
    args = parser.parse_args(argv)
    if args.command == "status":
        configuration = watchtower_configuration()
        print(json.dumps({"success": True, "configured": configuration["configured"], "url": configuration["url"]}))
        return 0
    result = trigger_update(args.reason, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
