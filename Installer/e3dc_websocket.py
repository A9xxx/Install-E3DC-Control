#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import websockets
import json
import logging

try:
    from live_snapshot import read_runtime_live_snapshot
except ImportError:  # pragma: no cover - Paketimport
    from Installer.live_snapshot import read_runtime_live_snapshot

logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(message)s")

CLIENTS = set()
LAST_DATA = ""
WEBSOCKET_BIND_HOST = "127.0.0.1"
WEBSOCKET_PORT = 8765

async def fetch_and_broadcast():
    global LAST_DATA
    while True:
        try:
            if CLIENTS:  # Nur abfragen, wenn auch jemand zuschaut (spart Ressourcen!)
                snapshot = await asyncio.to_thread(
                    read_runtime_live_snapshot,
                    live_max_age_s=15.0,
                    wallbox_max_age_s=30.0,
                    web_snapshot_max_age_s=180.0,
                    require_control_valid=True,
                    include_web_projection=True,
                )
                if not snapshot:
                    LAST_DATA = ""
                else:
                    new_data = json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )

                    # Nur senden, wenn sich die Daten geändert haben (Reduziert Traffic)
                    if new_data != LAST_DATA:
                        LAST_DATA = new_data
                        # An alle verbundenen Clients senden
                        for ws in list(CLIENTS):
                            try: await ws.send(new_data)
                            except Exception: pass
        except Exception:
            LAST_DATA = ""

        await asyncio.sleep(1) # Taktung für flüssige Animationen

async def handler(websocket, *args, **kwargs):
    client_ip = websocket.remote_address[0] if websocket.remote_address else "Unbekannt"
    logging.info(f"Neuer Client verbunden: {client_ip} (Gesamt: {len(CLIENTS)+1})")
    CLIENTS.add(websocket)
    if LAST_DATA:
        try: await websocket.send(LAST_DATA) # Sofort den letzten Stand senden
        except: pass

    try: await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        logging.info(f"Client getrennt: {client_ip} (Gesamt: {len(CLIENTS)})")

async def main():
    logging.info(
        "Starte E3DC WebSocket Server auf %s:%s...",
        WEBSOCKET_BIND_HOST,
        WEBSOCKET_PORT,
    )
    asyncio.create_task(fetch_and_broadcast())
    async with websockets.serve(handler, WEBSOCKET_BIND_HOST, WEBSOCKET_PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
