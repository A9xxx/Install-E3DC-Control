#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
import argparse
import socket
import ipaddress
from urllib.parse import urlsplit





def resolve_push_addresses(host):
    """Löst A und AAAA auf; als kleine Funktion ohne Netz prüfbar."""
    return [item[4][0] for item in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)]


def push_endpoint_is_public(endpoint, resolver=None):
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if parsed.scheme.lower() != "https" or parsed.username is not None or parsed.password is not None or "." not in host.rstrip("."):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        addresses = (resolver or resolve_push_addresses)(host)
        if not addresses:
            return False
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            blocked = ("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                       "192.168.0.0/16", "100.64.0.0/10", "224.0.0.0/4", "::/128", "::1/128", "fe80::/10", "fc00::/7", "ff00::/8")
            if any(ip in ipaddress.ip_network(network) for network in blocked):
                return False
        return True
    except (ValueError, OSError, TypeError):
        return False


def get_vapid_claims():
    """Liest VAPID-Keys aus e3dc_v4.json (Single Source of Truth)."""
    v4_path = "/var/www/html/data/e3dc_v4.json"
    try:
        with open(v4_path, "r", encoding='utf-8') as f:
            cfg = json.load(f)
            return cfg.get("push_vapid_private"), cfg.get("push_vapid_public")
    except:
        return None, None


def send_push_message(title, body, url=None, actions_json=None):
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        print(json.dumps({"error": f"python3-pywebpush Ladefehler: {str(e)}"}))
        return

    vapid_private, vapid_public = get_vapid_claims()
    if not vapid_private:
        print(json.dumps({"error": "VAPID private key nicht in e3dc_v4.json gefunden."}))
        return

    db_path = "/var/www/html/data/e3dc_stats.db"
    if not os.path.exists(db_path):
        print(json.dumps({"error": "Datenbank e3dc_stats.db existiert nicht."}))
        return

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions")
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(json.dumps({"error": f"Datenbank-Fehler: {e}"}))
        return

    if not rows:
        print(json.dumps({"error": "Keine registrierten Geräte gefunden."}))
        return

    payload_dict = {
        "title": title,
        "body": body,
        "url": url if url else "/"
    }
    if actions_json:
        try: payload_dict["actions"] = json.loads(actions_json)
        except: pass

    payload = json.dumps(payload_dict)

    success_count = 0
    skipped_count = 0
    errors = []

    for row in rows:
        if not push_endpoint_is_public(row["endpoint"]):
            skipped_count += 1
            continue
        subscription_info = {
            "endpoint": row["endpoint"],
            "keys": {
                "p256dh": row["p256dh"],
                "auth": row["auth"]
            }
        }

        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={
                    "sub": "mailto:operator@example.invalid"
                }
            )
            success_count += 1
        except WebPushException as ex:
            errors.append("Push-Dienst hat den Versand nicht bestätigt.")
            # Optional: Bei Gone (410) könnte man die Row löschen

    if success_count > 0:
        print(json.dumps({"success": True, "count": success_count, "skipped": skipped_count, "errors": errors}))
    else:
        print(json.dumps({"success": False, "count": 0, "skipped": skipped_count, "error": "Konnte an kein Gerät senden.", "errors": errors}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("title", nargs='?', default="E3DC-Control Notifikation")
    parser.add_argument("body", nargs='?', default="Dies ist eine Testnachricht.")
    parser.add_argument("--url", help="Action URL for the notification", default="/")
    parser.add_argument("--actions", help="JSON array of actions", default=None)
    args = parser.parse_args()

    send_push_message(args.title, args.body, args.url, args.actions)
