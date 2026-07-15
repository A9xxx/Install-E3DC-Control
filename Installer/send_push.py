#!/usr/bin/env python3
import os
import sys
import json
import sqlite3
import argparse




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
    errors = []
    
    for row in rows:
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
                    "sub": "mailto:admin@local.host"
                }
            )
            success_count += 1
        except WebPushException as ex:
            errors.append(repr(ex))
            # Optional: Bei Gone (410) könnte man die Row löschen
            
    if success_count > 0:
        print(json.dumps({"success": True, "count": success_count, "errors": errors}))
    else:
        print(json.dumps({"error": f"Konnte an kein Gerät senden. {errors}"}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("title", nargs='?', default="E3DC-Control Notifikation")
    parser.add_argument("body", nargs='?', default="Dies ist eine Testnachricht.")
    parser.add_argument("--url", help="Action URL for the notification", default="/")
    parser.add_argument("--actions", help="JSON array of actions", default=None)
    args = parser.parse_args()
    
    send_push_message(args.title, args.body, args.url, args.actions)
