"""Erstellt und publiziert den Wallbox-zu-Speicher-Intent-Vertrag."""

import json
import os
import time


def build_storage_intent_payload(data, *, now_ts=None, source="wallbox_manager"):
    payload = dict(data) if isinstance(data, dict) else {}
    payload.setdefault("schema_version", "wallbox_storage_intent_v2")
    payload["ts"] = int(time.time() if now_ts is None else float(now_ts))
    payload["source"] = str(source or "wallbox_manager")
    return payload


class StorageIntentPublisher:
    def __init__(self, path, logger=None):
        self.path = path
        self.logger = logger

    def publish(self, data, *, now_ts=None, source="wallbox_manager"):
        payload = build_storage_intent_payload(data, now_ts=now_ts, source=source)
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if self.logger is not None:
                self.logger.debug("Wallbox-Storage-Intent konnte nicht geschrieben werden: %s", exc)
            return False
