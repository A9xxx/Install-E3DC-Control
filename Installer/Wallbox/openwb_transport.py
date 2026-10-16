"""Gemeinsamer openWB-Transport: lokale Zertifikatsbindung vor Nutzdaten."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit, urljoin

MESSAGES = {
    "openwb_tls_required": "openWB verlangt HTTPS. Bitte Zertifikat im Wallbox-Abschnitt prüfen und bestätigen",
    "openwb_pin_changed": "openWB-Zertifikat geändert – Verbindung gesperrt",
    "openwb_certificate_expired": "openWB-Zertifikat abgelaufen oder noch nicht gültig – Verbindung gesperrt",
    "openwb_target_changed": "openWB-Ziel geändert – Zertifikat erneut prüfen und bestätigen",
    "openwb_auth_rejected": "openWB-Anmeldung abgelehnt",
    "openwb_acl_missing": "Verbindung hergestellt, erforderliche Leserechte fehlen",
    "openwb_redirect_rejected": "Unsichere openWB-Weiterleitung – Verbindung gesperrt",
    "openwb_address_invalid": "openWB-Adresse ungültig",
    "openwb_unavailable": "openWB-Verbindung nicht verfügbar",
    "openwb_mqtt_stale": "openWB-MQTT-Messwerte fehlen oder sind veraltet",
    "openwb_mqtt_unavailable": "openWB-MQTT-Verbindung nicht verfügbar",
    "openwb_mqtt_waiting": "openWB-MQTT verbunden; Leserechte noch nicht durch Datenempfang bestätigt",
    "openwb_cleartext_credentials": "openWB-Zugangsdaten benötigen HTTPS",
}


class TransportError(OSError):
    def __init__(self, reason):
        self.reason = reason if reason in MESSAGES else "openwb_unavailable"
        super().__init__(MESSAGES[self.reason])


class Address:
    def __init__(self, value):
        value = str(value or "").strip()
        try:
            if not value or re.search(r"[\s\\\x00-\x1f\x7f]", value):
                raise ValueError()
            self.explicit_http = value.lower().startswith("http://")
            parsed = urlsplit(value if "://" in value else "//" + value)
            if (parsed.scheme not in ("", "http", "https") or parsed.username is not None
                    or parsed.password is not None or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment or not parsed.hostname):
                raise ValueError()
            self.host = parsed.hostname.encode("idna").decode("ascii").lower()
            if "%" in self.host or any(c in self.host for c in "@/?#"):
                raise ValueError()
            self.scheme = parsed.scheme or "http"
            self.port = parsed.port
            if self.port is not None and not 1 <= self.port <= 65535:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise TransportError("openwb_address_invalid") from None
        self.authority = "[" + self.host + "]" if ":" in self.host else self.host
        # HTTP→HTTPS auf Standardports bleibt dieselbe Zielidentität.
        self.identity = self.authority + (":" + str(self.port) if self.port not in (None, 80, 443) else "")

    def base(self, tls=False, port=None):
        scheme = "https" if tls or self.scheme == "https" else "http"
        selected = port if port is not None else self.port
        if scheme == "https" and selected == 80:
            selected = 443
        return scheme + "://" + self.authority + (":" + str(selected) if selected else "")


def config_keys(slot):
    prefix = f"wb{int(slot)}_openwb_"
    return [prefix + key for key in ("username", "password", "tls_binding")]


def certificate_sha256(der):
    return hashlib.sha256(der).hexdigest()


def checked_binding(address, raw):
    if raw in (None, ""):
        return None
    try:
        binding = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if binding["target"] != address.identity:
            raise TransportError("openwb_target_changed")
        pem = binding["certificate"]
        fingerprint = binding["sha256"]
        if (not isinstance(pem, str) or len(pem) > 32768
                or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
                or not hmac.compare_digest(certificate_sha256(ssl.PEM_cert_to_DER_cert(pem)), fingerprint)
                or not float(binding["confirmed_at"]) > 0):
            raise ValueError()
        return binding
    except TransportError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise TransportError("openwb_pin_changed") from None


class OpenWBTransport:
    def __init__(self, address, config=None, slot=1):
        self.address = Address(address)
        self.config = dict(config or {})
        self.slot = int(slot)
        user_key, pass_key, binding_key = config_keys(slot)
        self.binding = checked_binding(self.address, self.config.get(binding_key))
        self.username = str(self.config.get(user_key) or "")
        self.password = str(self.config.get(pass_key) or "")
        # Alte HTTP-Konten behalten ihre Bedeutung; kein Slot-übergreifender Rückfall.
        self.http_user = str(self.config.get(f"wb{slot}_user") or
                             (self.config.get("wb_user") if self.slot == 1 else "") or "")
        self.http_password = str(self.config.get(f"wb{slot}_pass") or
                                 (self.config.get("wb_pass") if self.slot == 1 else "") or "")
        self.tls_required = bool(self.binding or self.address.scheme == "https" or self.username or self.password)
        self.resolved_base = self.address.base(tls=self.tls_required)

    def context(self):
        if self.binding:
            # Nur das ausdrücklich bestätigte Zertifikat ist Vertrauensanker.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False  # Die lokale Ziel-/DER-Bindung ersetzt den Snakeoil-Namen.
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=self.binding["certificate"])
            ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        else:
            ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def check_peer(self, sock):
        if self.binding and not hmac.compare_digest(
                certificate_sha256(sock.getpeercert(binary_form=True)), self.binding["sha256"]):
            raise TransportError("openwb_pin_changed")

    def wrap_socket(self, raw):
        wrapped = None
        try:
            wrapped = self.context().wrap_socket(raw, server_hostname=self.address.host)
            self.check_peer(wrapped)  # Vor erstem Header bzw. MQTT-CONNECT, bei jeder Verbindung.
            return wrapped
        except ssl.SSLCertVerificationError as exc:
            if exc.verify_code in (9, 10):
                reason = "openwb_certificate_expired"
            else:
                reason = "openwb_pin_changed" if self.binding else "openwb_tls_required"
            raise TransportError(reason) from None
        except Exception:
            raise
        finally:
            if sys.exc_info()[0] is not None:
                (wrapped or raw).close()

    def port(self, name, default):
        try:
            value = int(self.config.get(f"wb{self.slot}_openwb_{name}_port", default))
            if not 1 <= value <= 65535:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise TransportError("openwb_address_invalid") from None

    def mqtt_endpoint(self):
        if self.tls_required:
            return self.address.host, self.port("mqtt", 8883)
        # Ohne TLS-Bindung, Konto und https gilt eine bloße Adresse wie `http://` als
        # Bestand (5.5.4: MQTT 1883 ohne TLS). Eine erkannte HTTPS-Umleitung setzt
        # tls_required vorher, dann kein Klartext-Rückfall.
        return self.address.host, self.port("mqtt", 1883)

    def mqtt_client(self, mqtt, on_error):
        transport = self

        client = mqtt.Client()

        def checked_socket():
            raw = None
            try:
                # Paho ruft den Socket-Erzeuger bei jedem Reconnect vor CONNECT auf.
                raw = client._create_socket_connection()
                return transport.wrap_socket(raw) if transport.tls_required else raw
            except Exception as exc:
                if raw is not None:
                    raw.close()
                on_error(exc.reason if isinstance(exc, TransportError) else "openwb_mqtt_unavailable")
                raise OSError(MESSAGES.get(getattr(exc, "reason", ""), MESSAGES["openwb_mqtt_unavailable"])) from None

        client._create_socket = checked_socket
        if self.username:
            client.username_pw_set(self.username, self.password)
        return client

    def redirect(self, source, location):
        try:
            target = urlsplit(urljoin(source, location))
            if (target.scheme != "https" or target.hostname != self.address.host
                    or target.username is not None or target.password is not None
                    or target.fragment or not 1 <= (target.port or 443) <= 65535
                    or re.search(r"[\s\\\x00-\x1f\x7f]", location)):
                raise ValueError()
            # Keine beliebigen Pfade/Abfragen aus einer Umleitungsantwort übernehmen.
            original = urlsplit(source)
            if target.path != original.path or target.query != original.query:
                raise ValueError()
            return "https://" + target.netloc
        except (ValueError, TypeError):
            raise TransportError("openwb_redirect_rejected") from None

    def _exchange(self, base, path, method, body, headers, timeout):
        parsed = urlsplit(base)
        tls = parsed.scheme == "https"
        if not tls and self.tls_required:
            raise TransportError("openwb_tls_required")
        if not tls and any(k.lower() in ("authorization", "cookie", "proxy-authorization") for k in headers):
            raise TransportError("openwb_cleartext_credentials")
        conn = http.client.HTTPConnection(self.address.host, parsed.port or (443 if tls else 80), timeout=timeout)
        try:
            conn.connect()
            if tls:
                conn.sock = self.wrap_socket(conn.sock)
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            # Eine bereits gelesene Umleitung bleibt auch bei defektem Body
            # erkennbar. Ihr Inhalt wird für die Zielprüfung nicht benötigt.
            if 300 <= response.status < 400:
                return response.status, response.getheader("Location", ""), b""
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise TransportError("openwb_unavailable")
            return response.status, response.getheader("Location", ""), data
        finally:
            conn.close()

    def request(self, path, *, method="GET", body=None, headers=None, v1=False, timeout=5, protection=False):
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise TransportError("openwb_address_invalid")
        self.last_request_reason = ""
        headers = dict(headers or {})
        # Authentifizierung wird ausschließlich hier nach der Zielauflösung ergänzt.
        headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "proxy-authorization", "cookie")}
        base = self.address.base(tls=True, port=self.port("v1", 8443)) if v1 else self.resolved_base
        try:
            # Auflösung vor jedem unverschlüsselten Schreibaufruf, ohne Nutzdaten.
            if method != "GET" and urlsplit(base).scheme == "http":
                # Nur ein Schutzbefehl ohne TLS-/Kontopflicht darf bei einem
                # fehlgeschlagenen Leseweg einmal den Schreibkanal versuchen.
                fallback_allowed = bool(protection and not self.tls_required and not self.http_user)
                try:
                    status, location, _ = self._exchange(base, path, "GET", None, {}, timeout)
                except Exception as exc:
                    if (not fallback_allowed or isinstance(exc, TransportError)
                            and exc.reason != "openwb_unavailable"):
                        raise
                    status, location = None, ""
                if status in (301, 302, 303, 307, 308):
                    if protection:
                        if status == 301:
                            # Erkannte TLS-Pflicht auch für spätere Befehle und
                            # den MQTT-Kanal erhalten, ohne den POST umzuleiten.
                            base = self.redirect(base + path, location)
                            self.tls_required = True
                            self.resolved_base = base
                        raise TransportError("openwb_redirect_rejected")
                    if status != 301:
                        raise TransportError("openwb_redirect_rejected")
                    base = self.redirect(base + path, location)
                    self.tls_required = True
                    self.resolved_base = base
                elif status != 200:
                    if not fallback_allowed or status is not None and 300 <= status < 400:
                        raise TransportError("openwb_unavailable")
                    self.last_request_reason = "openwb_protection_post_after_failed_probe"
            for attempt in range(1 if self.last_request_reason else 2):
                tls = urlsplit(base).scheme == "https"
                user = self.username if v1 and self.username else self.http_user
                password = self.password if v1 and self.username else self.http_password
                if user and tls:
                    headers["Authorization"] = "Basic " + base64.b64encode((user + ":" + password).encode()).decode()
                elif user and method != "GET":
                    raise TransportError("openwb_cleartext_credentials")
                status, location, data = self._exchange(base, path, method, body, headers, timeout)
                if status in (301, 302, 303, 307, 308):
                    if status != 301 or attempt or method != "GET":
                        raise TransportError("openwb_redirect_rejected")
                    base = self.redirect(base + path, location)
                    self.tls_required = True
                    self.resolved_base = base
                    continue
                if status == 401:
                    raise TransportError("openwb_auth_rejected")
                if status == 403:
                    raise TransportError("openwb_acl_missing")
                if not 200 <= status < 300:
                    raise TransportError("openwb_unavailable")
                return data.decode("utf-8", errors="replace")
        except TransportError:
            raise
        except Exception:
            raise TransportError("openwb_unavailable") from None
        raise TransportError("openwb_redirect_rejected")


def probe_certificate(address, *, port=None, timeout=3):
    """Nur manuelle Ermittlung: kein HTTP/MQTT, kein Konto, keine Freigabe."""
    target = Address(address)
    raw = socket.create_connection((target.host, port or (target.port if target.port not in (None, 80) else 443)), timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Ausschließlich die unbestätigte Anzeige, niemals Nutzdaten.
    with raw:
        with ctx.wrap_socket(raw, server_hostname=target.host) as sock:
            der = sock.getpeercert(binary_form=True)
    return {"target": target.identity, "sha256": certificate_sha256(der),
            "certificate": ssl.DER_cert_to_PEM_cert(der)}


def confirm_certificate(address, candidate, fingerprint, *, now=None):
    if (not isinstance(candidate, dict) or candidate.get("target") != Address(address).identity
            or not hmac.compare_digest(str(candidate.get("sha256", "")), str(fingerprint))):
        raise TransportError("openwb_target_changed")
    current = probe_certificate(address)
    if current != candidate:
        raise TransportError("openwb_pin_changed")
    binding = dict(current, confirmed_at=time.time() if now is None else float(now))
    transport = OpenWBTransport(address, {"wb1_openwb_tls_binding": json.dumps(binding)})
    # Gültigkeit und Signatur vor dem Speichern, weiterhin ohne Nutzdaten.
    raw = socket.create_connection((transport.address.host, urlsplit(transport.resolved_base).port or 443), 3)
    with transport.wrap_socket(raw):
        pass
    return json.dumps(binding, separators=(",", ":"))


def main():
    try:
        args = json.load(sys.stdin)
        action = args.get("action")
        address = args.get("address")
        if action == "probe":
            result = {"candidate": probe_certificate(address)}
        elif action == "confirm":
            result = {"binding": confirm_certificate(address, args.get("candidate"), args.get("sha256"))}
        elif action == "discover":
            transport = OpenWBTransport(address, args.get("config"), int(args.get("slot", 1)))
            data = json.loads(transport.request("/openWB/simpleAPI/simpleapi.php?get_chargepoint_all", timeout=1.2))
            result = {"payload": data}
        else:
            raise TransportError("openwb_unavailable")
        print(json.dumps(dict(success=True, **result)))
    except Exception as exc:
        reason = exc.reason if isinstance(exc, TransportError) else "openwb_unavailable"
        print(json.dumps({"success": False, "reason": reason, "message": MESSAGES[reason]}))


if __name__ == "__main__":
    main()
